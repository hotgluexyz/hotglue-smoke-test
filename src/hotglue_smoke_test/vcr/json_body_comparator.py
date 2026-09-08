import json
import logging
from deepdiff import DeepDiff
from vcr.matchers import body

# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class JsonBodyComparator:
    def __init__(self, test_config):
        self.test_config = test_config

    def compare(self, actual_request, expected_request):
        if self.test_config and self.test_config.get("ignore_columns"):
            streams_with_ignored_columns= self.test_config["ignore_columns"].keys()
            if any(key in expected_request.path for key in streams_with_ignored_columns) and any(key in actual_request.path for key in streams_with_ignored_columns):
                json_expected_body = json.loads(expected_request.body)
                json_actual_body = json.loads(actual_request.body)
                # Find the matching key for the expected request path
                matching_key = next(key for key in streams_with_ignored_columns if key in expected_request.path)
                ignored_keys = self.test_config["ignore_columns"][matching_key]
                comparable_expected_body = self.construct_comparable_json(json_expected_body, ignored_keys)
                comparable_actual_body = self.construct_comparable_json(json_actual_body, ignored_keys)
                diff = DeepDiff(comparable_expected_body, comparable_actual_body)
                if len(diff) > 0:
                    raise AssertionError(f"Test FAILED!! Differences found in body:\n{diff}")
        else:
            body(actual_request, expected_request)


    def construct_comparable_json(self, json_body, ignore_keys):
        """
        Construct a comparable JSON object from a JSON body.

        This function will remove all keys that are not in the list of keys to compare.

        Args:
            json_body (dict): The JSON body to construct a comparable object from.

        Returns:
            dict: A comparable JSON object.
        """

        def remove_ignored_columns(body, ignore_columns):
            """
            Remove specified columns from a record, including nested fields.

            Args:
                body (dict): The body to process.
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
                remove_nested_key(body, keys)

            return body

        return remove_ignored_columns(json_body, ignore_keys)

