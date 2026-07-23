import json
import os
from collections import defaultdict
from deepdiff import DeepDiff

class SingerOutputComparator:
    def __init__(self, expected_output_dir, actual_output_dir, test_config):
        """
        Initialize the comparator with a unittest.TestCase instance and configuration.

        Args:
            expected_output_dir (str): Directory containing the expected output.
            actual_output_dir (str): Directory containing the actual output.
            test_config (dict): Configuration for comparison.
        """
        self.expected_output_dir = expected_output_dir
        self.actual_output_dir = actual_output_dir
        self.test_config = test_config

    def _validate_output_directory(self, directory):
        """
        Validate that the directory exists and contains a single 'data.singer' file.
        Args:
            directory (str): The directory to validate.
        """
        assert os.path.isdir(directory), f"The directory '{directory}' does not exist."
        data_singer = os.path.join(directory, "data.singer")
        assert os.path.isfile(data_singer), (
            f"The directory '{directory}' must contain a 'data.singer' file."
        )

    def _validate_singer_schema(self, file_path):
        """
        Validate Singer schema and output key attributes for each stream.

        Args:
            file_path (str): Path to the Singer file.

        Returns:
            dict: Schema information organized by stream, containing key attributes.
        """
        schemas_by_stream = {}
        
        with open(file_path, 'r') as f:
            for line in f:
                data = json.loads(line.strip())
                
                if data.get("type") == "SCHEMA":
                    stream_name = data["stream"]
                    schema = data["schema"]
                    
                    # Extract key attributes from the schema
                    key_attributes = []
                    
                    # Check for key properties in the schema
                    if "key_properties" in data:
                        key_attributes = data["key_properties"]
                    elif "properties" in schema:
                        # Look for fields marked as key or primary key
                        properties = schema["properties"]
                        for prop_name, prop_details in properties.items():
                            if isinstance(prop_details, dict):
                                # Check if the property is marked as a key
                                if prop_details.get("key") or prop_details.get("primary_key"):
                                    key_attributes.append(prop_name)
                    
                    schemas_by_stream[stream_name] = {
                        "key_attributes": key_attributes,
                        "schema": schema,
                        "bookmark_properties": data.get("bookmark_properties", [])
                    }

                    print(f"Stream: {stream_name}")
                    print(f"  Key Attributes: {key_attributes}")
                    print(f"  Bookmark Properties: {data.get('bookmark_properties', [])}")
                    print()
        
        return schemas_by_stream

    def _read_singer_file(self, file_path, ignore_streams, ignore_columns_config):
        """
        Read a Singer file and organize records by stream, excluding ignored columns.

        Args:
            file_path (str): Path to the Singer file.
            ignore_streams (list): Streams to ignore.
            ignore_columns_config (dict): A dictionary specifying columns to ignore for each stream.

        Returns:
            dict: Records organized by stream, with ignored columns removed.
        """
        def remove_ignored_columns(record, ignore_columns):
            """
            Remove specified columns from a record, including nested fields.

            Args:
                record (dict): The record to process.
                ignore_columns (list): A list of column names to remove (supports dot notation).

            Returns:
                dict: A record with ignored columns removed.
            """
            def remove_nested_key(current, keys):
                """
                Recursively remove a nested key specified by dot notation.

                Args:
                    current (dict): The current level of the dictionary.
                    keys (list): A list of keys representing the path to the value to remove.
                """
                if len(keys) == 1:  # Base case: remove the final key
                    if isinstance(current, dict):
                        current.pop(keys[0], None)
                else:  # Recursive case: traverse to the next level
                    if isinstance(current, dict) and keys[0] in current:
                        if isinstance(current[keys[0]], list):  # Handle lists of dictionaries
                            for item in current[keys[0]]:
                                if isinstance(item, dict):
                                    remove_nested_key(item, keys[1:])
                        elif isinstance(current[keys[0]], dict):  # Handle nested dictionaries
                            remove_nested_key(current[keys[0]], keys[1:])

            # Iterate over all columns to ignore and remove them
            for column in ignore_columns:
                keys = column.split('.')
                remove_nested_key(record, keys)

            return record

        records_by_stream = defaultdict(list)
        with open(file_path, 'r') as f:
            for line in f:
                data = json.loads(line.strip())
                if data.get("type") == "RECORD" and data["stream"] not in ignore_streams:
                    stream_name = data["stream"]
                    ignore_columns = ignore_columns_config.get(stream_name, [])
                    # Remove ignored columns from the record
                    filtered_record = remove_ignored_columns(data["record"], ignore_columns)
                    records_by_stream[stream_name].append(filtered_record)
        return records_by_stream

    def _validate_stream_names(self, expected_streams, actual_streams):
        """
        Validate that the same streams are found in both expected and actual streams.
        Args:
            expected_streams (dict): The dictionary of expected streams.
            actual_streams (dict): The dictionary of actual streams.
        """
        expected_stream_names = set(expected_streams.keys())
        actual_stream_names = set(actual_streams.keys())

        # Assert that the streams are the same
        assert actual_stream_names == expected_stream_names, (
            f"Stream comparison mismatch:\n"
            f"Streams in expected but not in actual: {expected_stream_names - actual_stream_names}\n"
            f"Streams in actual but not in expected: {actual_stream_names - expected_stream_names}"
        )

    def _rename_columns(self, records, rename_config):
        """
        Rename specified columns in the records based on the given configuration.

        Args:
            records (list): A list of records (dictionaries) to process.
            rename_config (dict): A dictionary where keys are old column names (dot notation supported)
                                and values are new column names.

        Returns:
            list: Records with columns renamed.
        """
        def rename_nested_key(record, old_key, new_key):
            """
            Rename a nested key in a record.

            Args:
                record (dict): The record to modify.
                old_key (str): The old key in dot notation.
                new_key (str): The new key in dot notation.
            """
            old_keys = old_key.split('.')
            new_keys = new_key.split('.')

            # Traverse to the parent of the old key
            current = record
            for key in old_keys[:-1]:
                if isinstance(current, dict) and key in current:
                    current = current[key]
                else:
                    return  # If the path doesn't exist, do nothing

            # Get the value of the old key
            if isinstance(current, dict) and old_keys[-1] in current:
                value = current.pop(old_keys[-1])  # Remove the old key

                # Traverse to the parent of the new key
                current = record
                for key in new_keys[:-1]:
                    if isinstance(current, dict):
                        if key not in current:
                            current[key] = {}  # Create nested dictionaries as needed
                        current = current[key]

                # Set the value of the new key
                if isinstance(current, dict):
                    current[new_keys[-1]] = value

        # Apply renaming for each record
        for record in records:
            for old_key, new_key in rename_config.items():
                rename_nested_key(record, old_key, new_key)

        return records

    def _sort_streams(self, streams, sort_config):
        """
        Sort the streams based on the given sort configuration.

        Args:
            streams (dict): The streams to sort, where each key is a stream name and the value is a list of records.
            sort_config (dict): A dictionary where keys are stream names and values are lists of attributes to sort by.
        """
        def resolve_nested_key(item, key):
            """
            Resolve a nested key (dot notation) in a dictionary.

            Args:
                item (dict): The dictionary to traverse.
                key (str): The key in dot notation to resolve.

            Returns:
                Any: The value corresponding to the nested key, or None if not found.
            """
            keys = key.split('.')
            for k in keys:
                if isinstance(item, dict) and k in item:
                    item = item[k]
                else:
                    return None
            return item

        for stream_name, records in streams.items():
            if stream_name in sort_config:
                sort_fields = sort_config[stream_name]

                # Step 1: Sort outermost rows by flat fields
                flat_sort_keys = [field for field in sort_fields if '.' not in field]
                if flat_sort_keys:
                    def flat_sort_key(record):
                        return tuple(record.get(field) for field in flat_sort_keys)
                    records.sort(key=flat_sort_key)

                # Step 2: Sort nested lists of dictionaries
                nested_dict_fields = [field for field in sort_fields if '.' in field and not field.endswith('.')]
                for field in nested_dict_fields:
                    outer_field, inner_field = field.split('.', 1)  # Split into outer field and nested key
                    for record in records:
                        if isinstance(record.get(outer_field), list):
                            record[outer_field].sort(key=lambda x: resolve_nested_key(x, inner_field))

                # Step 3: Sort nested lists of scalars
                scalar_list_fields = [field for field in sort_fields if field.endswith('.')]
                for field in scalar_list_fields:
                    scalar_field = field[:-1]  # Remove the trailing dot
                    for record in records:
                        if isinstance(record.get(scalar_field), list):
                            record[scalar_field].sort()

    def _validate_record_count(self, stream_name, expected_records, actual_records):
        """
        Validate that the record count is the same for a given stream in both expected and actual records.

        Args:
            stream_name (str): The name of the stream being validated.
            expected_records (list): The expected records for the stream.
            actual_records (list): The actual records for the stream.
        """
        assert len(actual_records) == len(expected_records), (
            f"Stream '{stream_name}' record count mismatch:\n"
            f"Expected: {len(expected_records)}, Actual: {len(actual_records)}"
        )

        print(f"SUCCESS!! Stream [{stream_name}], Count matched successfully; Expected: {len(expected_records)}, Actual: {len(actual_records)} ")

    def _validate_no_duplicates(self, stream_name, records, key_attributes):
        """
        Validate that there are no duplicate records based on key properties.

        Args:
            stream_name (str): The name of the stream being validated.
            records (list): The records to check for duplicates.
            key_attributes (list): The key attributes to use for duplicate detection.
        """
        if not key_attributes or key_attributes is None:
            print(f"WARNING: No key attributes defined for stream '{stream_name}', skipping duplicate validation.")
            return

        def get_key_values(record):
            """
            Extract key values from a record based on key attributes.
            
            Args:
                record (dict): The record to extract key values from.
                
            Returns:
                tuple: A tuple of key values for duplicate detection.
            """
            key_values = []
            for attr in key_attributes:
                # Handle nested attributes using dot notation
                keys = attr.split('.')
                value = record
                for key in keys:
                    if isinstance(value, dict) and key in value:
                        value = value[key]
                    else:
                        value = None
                        break
                key_values.append(value)
            return tuple(key_values)

        # Create a set to track seen key combinations
        seen_keys = set()
        duplicates = []
        
        for index, record in enumerate(records):
            key_values = get_key_values(record)
            
            if key_values in seen_keys:
                duplicates.append({
                    'index': index,
                    'record': record,
                    'key_values': key_values
                })
            else:
                seen_keys.add(key_values)

        # Assert no duplicates found
        assert not duplicates, (
            f"Duplicate records found in stream '{stream_name}' based on key attributes {key_attributes}:\n"
            f"Found {len(duplicates)} duplicate(s):\n" +
            "\n".join([
                f"  Record {dup['index']}: {dup['key_values']} -> {dup['record']}"
                for dup in duplicates
            ])
        )

        print(f"SUCCESS!! Stream [{stream_name}], No duplicates found based on key attributes: {key_attributes}")

    def _validate_key_attributes_exist(self, stream_name, records, key_attributes, ignore_columns_config):
        """
        Validate that all key attributes exist in the records.

        Args:
            stream_name (str): The name of the stream being validated.
            records (list): The records to validate.
            key_attributes (list): The key attributes to check for existence.
        """
        if not records:
            return  # No records to validate
        
        if not key_attributes or key_attributes is None:
            print(f"WARNING: No key attributes defined for stream '{stream_name}', skipping key attribute validation.")
            return

        missing_attributes = []
        for attr in key_attributes:
            if attr in ignore_columns_config:
                continue
            # Check if the attribute exists in all records
            for index, record in enumerate(records):
                keys = attr.split('.')
                value = record
                for key in keys:
                    # Key present with JSON null is valid; only absent keys are missing.
                    if isinstance(value, dict) and key in value:
                        value = value[key]
                    else:
                        missing_attributes.append({
                            'attribute': attr,
                            'record_index': index,
                            'record': record
                        })
                        break

        assert not missing_attributes, (
            f"Missing key attributes in stream '{stream_name}':\n"
            + "\n".join([
                f"  Attribute '{missing['attribute']}' missing in record {missing['record_index']}"
                for missing in missing_attributes
            ])
        )

    def compare(self):
        self._validate_output_directory(self.expected_output_dir)
        self._validate_output_directory(self.actual_output_dir)

        ignore_files = self.test_config.get("ignore_files")
        sort_config = self.test_config.get("sort_config")
        ignore_columns_config = self.test_config.get("ignore_columns")
        rename_config = self.test_config.get("rename_config")
        skip_duplicate_validation = self.test_config.get("skip_duplicate_validation", False)

        # Get schema information for key attributes
        expected_file = os.path.join(self.expected_output_dir, "data.singer")
        actual_file = os.path.join(self.actual_output_dir, "data.singer")
        expected_schemas = self._validate_singer_schema(expected_file)
        actual_schemas = self._validate_singer_schema(actual_file)

        expected_streams = self._read_singer_file(
            os.path.join(self.expected_output_dir, "data.singer"), ignore_files, ignore_columns_config
        )
        actual_streams = self._read_singer_file(
            os.path.join(self.actual_output_dir, "data.singer"), ignore_files, ignore_columns_config
        )

        # Apply renaming to actual_streams
        for stream_name, records in actual_streams.items():
            if stream_name in rename_config:
                actual_streams[stream_name] = self._rename_columns(records, rename_config[stream_name])

        self._validate_stream_names(expected_streams, actual_streams)

        self._sort_streams(actual_streams, sort_config)
        self._sort_streams(expected_streams, sort_config)

        for stream_name, actual_stream_values in actual_streams.items():
            expected_stream_values = expected_streams[stream_name]

            self._validate_record_count(stream_name, expected_stream_values, actual_stream_values)
            
            if skip_duplicate_validation:
                print(f"Skipping duplicate validation for stream '{stream_name}'")
            else:
                # Validate no duplicates based on key attributes for both expected and actual
                if stream_name in actual_schemas:
                    key_attributes = actual_schemas[stream_name]["key_attributes"]
                    
                    # First validate that key attributes exist in the records
                    self._validate_key_attributes_exist(stream_name, actual_stream_values, key_attributes, ignore_columns_config.get(stream_name, []))
                    
                    # Check actual records for duplicates
                    self._validate_no_duplicates(stream_name, actual_stream_values, key_attributes)
                    
                    # Also check expected records for duplicates
                    if stream_name in expected_schemas:
                        expected_key_attributes = expected_schemas[stream_name]["key_attributes"]
                        
                        assert expected_key_attributes == key_attributes, (
                            f"Key attributes mismatch between expected and actual for stream '{stream_name}':\n"
                            f"  Expected: {expected_key_attributes}\n"
                            f"  Actual: {key_attributes}"
                        )

                        self._validate_key_attributes_exist(f"{stream_name} (expected)", expected_stream_values, expected_key_attributes, ignore_columns_config.get(stream_name, []))
                        self._validate_no_duplicates(f"{stream_name} (expected)", expected_stream_values, expected_key_attributes)
                else:
                    print(f"⚠️: No schema information found for stream '{stream_name}', skipping duplicate validation.")

            for index, (expected_record, actual_record) in enumerate(zip(expected_stream_values, actual_stream_values)):
                differences = DeepDiff(expected_record, actual_record, ignore_order=True)
                assert not differences, (
                    f"Differences found in stream '{stream_name}' at record index {index}:\n{differences}"
                )

            print(f"SUCCESS!! Stream [{stream_name}], Content matched successfully.")
            
