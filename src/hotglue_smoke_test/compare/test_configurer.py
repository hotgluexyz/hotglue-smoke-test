import json
import os

class TestConfigurer():
    @staticmethod
    def get_test_config(root_dir):

        test_config = {
            "ignore_columns": {},
            "ignore_files": [],
            "sort_config": {},
            "rename_config": {},
        }

        def override_dict(_test_config, attr):
            if attr in _test_config:
                test_config[attr].update(_test_config[attr])

        def override_array(_test_config, attr):
            if attr in _test_config:
                test_config[attr] = list(set(test_config[attr] + _test_config[attr]))

        def load_test_config(test_config_file):
            if not os.path.exists(test_config_file):
                return

            with open(test_config_file, 'r') as f:
                _test_config = json.load(f)

            override_dict(_test_config, "ignore_columns")
            override_array(_test_config, "ignore_files")
            override_dict(_test_config, "sort_config")
            override_dict(_test_config, "rename_config")

            # Handle any other keys not specifically processed above
            handled_keys = {"ignore_columns", "ignore_files", "sort_config", "rename_config"}
            for key, value in _test_config.items():
                if key not in handled_keys:
                    test_config[key] = value

        tap_test_config = os.path.join(os.path.dirname(root_dir), 'test-config.json')
        case_test_config = os.path.join(root_dir, 'test-config.json')

        load_test_config(case_test_config)
        load_test_config(tap_test_config)

        return test_config
