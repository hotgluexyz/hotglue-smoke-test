import json
import os
from deepdiff import DeepDiff

class StateOutputComparator:
    def __init__(self, expected_output_dir, actual_output_dir, test_config):
        """
        Initialize the comparator.

        Args:
            expected_output_dir (str): Directory containing the expected output state.
            actual_output_dir (str): Directory containing the actual output state.
            test_config (dict): Configuration specifying ignored keys.
        """
        self.expected_output_dir = expected_output_dir
        self.actual_output_dir = actual_output_dir
        self.ignore_keys = test_config.get("ignore_files", [])

    def _validate_state_file(self, directory):
        """
        Validate that the directory contains a `state.json` file.

        Args:
            directory (str): The directory to validate.

        Returns:
            str: Path to the `state.json` file.

        Raises:
            FileNotFoundError: If `state.json` is missing.
        """
        state_file_path = os.path.join(directory, "state.json")
        if not os.path.exists(state_file_path):
            raise FileNotFoundError(f"Missing required file: {state_file_path}")
        return state_file_path

    def _load_state(self, file_path):
        """
        Load and parse the JSON from the state file.

        Args:
            file_path (str): Path to the `state.json` file.

        Returns:
            dict: Parsed state data.
        """
        with open(file_path, "r") as f:
            return json.load(f)

    def _remove_ignored_keys(self, data):
        """
        Remove ignored keys from the state JSON.

        Args:
            data (dict): The state JSON.

        Returns:
            dict: Filtered state JSON without ignored keys.
        """
        if not self.ignore_keys:
            return data  # No ignored keys, return data as is

        def remove_keys(d, keys_to_ignore):
            """Recursively remove ignored keys from a dictionary."""
            if isinstance(d, dict):
                return {k: remove_keys(v, keys_to_ignore) for k, v in d.items() if k not in keys_to_ignore}
            elif isinstance(d, list):
                return [remove_keys(i, keys_to_ignore) for i in d]
            return d

        return remove_keys(data, self.ignore_keys)

    def compare(self):
        """
        Compare the actual and expected state JSON files.

        Raises:
            AssertionError: If differences are found.
        """
        # Validate state.json files exist
        expected_file = self._validate_state_file(self.expected_output_dir)
        actual_file = self._validate_state_file(self.actual_output_dir)

        # Load state files
        expected_state = self._load_state(expected_file)
        actual_state = self._load_state(actual_file)

        # Remove ignored keys
        expected_state_filtered = self._remove_ignored_keys(expected_state)
        actual_state_filtered = self._remove_ignored_keys(actual_state)

        # Compare using DeepDiff
        differences = DeepDiff(expected_state_filtered, actual_state_filtered, ignore_order=True)

        assert not differences, (
            f"Differences found in state.json:\n{differences}"
        )

        print("SUCCESS!! State output matched expected output.")
