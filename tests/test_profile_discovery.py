import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.profiles import discover_profiles, ordered_validation_groups, ordered_validation_plan


OPENAPI = """openapi: 3.0.0
paths:
  /shacl/validation/report:
    post:
      parameters:
        - name: shapeModel
          in: query
          schema:
            type: string
            enum:
              - dcatap301level1
              - dcatap301level2
              - dcatap301orig
              - dcatapde20
              - dcatap301
"""


class ProfileDiscoveryTests(unittest.TestCase):
    def test_reads_only_local_yaml_and_filters_german_and_orig(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openapi-shacl.yaml"
            path.write_text(OPENAPI, encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                source = discover_profiles(path)
        self.assertEqual(source.profiles, [
            "dcatap301level1", "dcatap301level2", "dcatap301"
        ])
        self.assertTrue(source.source.startswith("file:"))

    def test_unsuffixed_all_shapes_profile_is_last_in_its_chain(self):
        profiles = [
            "dcatap301level2", "dcatap300level1", "dcatap301",
            "dcatap301level1", "dcatap300", "dcatap300level3",
        ]
        groups = ordered_validation_groups(profiles)
        self.assertEqual(groups, [
            ["dcatap301level1", "dcatap301level2", "dcatap301"],
            ["dcatap300level1", "dcatap300level3", "dcatap300"],
        ])
        self.assertEqual(ordered_validation_plan(profiles), [item for group in groups for item in group])

    def test_missing_local_yaml_fails_instead_of_falling_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-openapi-shacl.yaml"
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(FileNotFoundError):
                    discover_profiles(missing)

    def test_environment_variable_selects_local_yaml_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "openapi-shacl.yaml"
            path.write_text(OPENAPI, encoding="utf-8")
            with patch.dict(os.environ, {"MQA_SHACL_OPENAPI": str(path)}, clear=True):
                source = discover_profiles()
        self.assertEqual(source.profiles, [
            "dcatap301level1", "dcatap301level2", "dcatap301"
        ])


if __name__ == "__main__":
    unittest.main()
