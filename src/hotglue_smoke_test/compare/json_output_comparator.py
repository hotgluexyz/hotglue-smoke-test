import glob
import json
import os

from deepdiff import DeepDiff


class JsonOutputComparator:
    def __init__(self, testname, expected_output_dir, actual_output_dir, test_config):
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
        self.testname = testname

    def _validate_output_directory(self, directory):
        """
        Validate that the directory exists and contains at least one JSON file.
        Args:
            directory (str): The directory to validate.
        """
        # Assert the directory exists
        assert os.path.isdir(directory), f"Test [{self.testname}] FAILED!! The directory '{directory}' does not exist."

        # Validate that there is at least one JSON file in the directory
        json_files = glob.glob(os.path.join(directory, "*.json"))

        if len(json_files) == 0:
            print(f"Test [{self.testname}], has no json files to compare in dir {directory} ")
            return

        assert len(json_files) >= 1, (
            f"Test [{self.testname}] FAILED!! The directory '{directory}' must contain at least one JSON file. "
            f"Found {len(json_files)} file(s)."
        )

    def _read_json_folder(self, folder_path, ignore_streams, ignore_columns_config):  # noqa: C901
        """
        Read multiple JSON files in a folder and organize records by stream, excluding ignored columns.

        Args:
            folder_path (str): Path to the folder containing JSON files.
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

        # Initialize the result dictionary to hold stream records
        all_streams_data = {}

        # Iterate over the files in the provided folder path
        for filename in os.listdir(folder_path):
            if filename.endswith(".json"):
                # Construct the full path to the JSON file
                file_path = os.path.join(folder_path, filename)

                # Skip files that correspond to streams to be ignored
                stream_name = filename[:-5]  # Remove '.json' to get stream name
                if stream_name in ignore_streams:
                    continue

                # Read and parse the content of the file
                with open(file_path, 'r') as file:
                    records = json.load(file)

                    # If there are columns to ignore for the current stream, apply them
                    if stream_name in ignore_columns_config:
                        ignore_columns = ignore_columns_config[stream_name]
                        for record in records:
                            remove_ignored_columns(record, ignore_columns)

                    # Add the processed records to the dictionary under the stream name
                    all_streams_data[stream_name] = records

        return all_streams_data


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
            f"Test [{self.testname}] FAILED!! Stream comparison mismatch:\n"
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
            f"Test [{self.testname}] FAILED!! Stream '{stream_name}' record count mismatch:\n"
            f"Expected: {len(expected_records)}, Actual: {len(actual_records)}"
        )

        print(f"SUCCESS!! Stream [{stream_name}], Count   matched successfully; Expected: {len(expected_records)}, Actual: {len(actual_records)} ")



    def compare(self):
        self._validate_output_directory(self.expected_output_dir)
        self._validate_output_directory(self.actual_output_dir)

        ignore_files = self.test_config.get("ignore_files") or []
        sort_config = self.test_config.get("sort_config") or {}
        ignore_columns_config = self.test_config.get("ignore_columns") or {}
        rename_config = self.test_config.get("rename_config") or {}

        expected_streams = self._read_json_folder(
            self.expected_output_dir, ignore_files, ignore_columns_config
        )
        actual_streams = self._read_json_folder(
            self.actual_output_dir, ignore_files, ignore_columns_config
        )

        # Apply renaming to actual_streams
        for stream_name, records in actual_streams.items():
            if stream_name in rename_config:
                actual_streams[stream_name] = self._rename_columns(
                    records, rename_config[stream_name]
                )

        self._validate_stream_names(expected_streams, actual_streams)

        self._sort_streams(actual_streams, sort_config)
        self._sort_streams(expected_streams, sort_config)

        for stream_name, actual_stream_values in actual_streams.items():
            expected_stream_values = expected_streams[stream_name]

            self._validate_record_count(
                stream_name, expected_stream_values, actual_stream_values
            )

            for index, (expected_record, actual_record) in enumerate(
                zip(expected_stream_values, actual_stream_values)
            ):
                differences = DeepDiff(
                    expected_record, actual_record, ignore_order=True
                )
                assert not differences, (
                    f"Test [{self.testname}] FAILED!! Differences found in stream "
                    f"'{stream_name}' at record index {index}:\n{differences}"
                )

            print(
                f"SUCCESS!! Stream [{stream_name}], Content matched successfully."
            )
