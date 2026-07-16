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

        case_test_config = os.path.join(root_dir, 'test-config.json')
        if not os.path.exists(case_test_config):
            return test_config

        with open(case_test_config, 'r') as f:
            _test_config = json.load(f)

        if "ignore_columns" in _test_config:
            test_config["ignore_columns"].update(_test_config["ignore_columns"])
        if "ignore_files" in _test_config:
            test_config["ignore_files"] = list(set(test_config["ignore_files"] + _test_config["ignore_files"]))
        if "sort_config" in _test_config:
            test_config["sort_config"].update(_test_config["sort_config"])
        if "rename_config" in _test_config:
            test_config["rename_config"].update(_test_config["rename_config"])

        handled_keys = {"ignore_columns", "ignore_files", "sort_config", "rename_config"}
        for key, value in _test_config.items():
            if key not in handled_keys:
                test_config[key] = value

        return test_config
