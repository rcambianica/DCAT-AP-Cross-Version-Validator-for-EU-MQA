import io
import unittest
from unittest.mock import patch

from src.cli import build_parser, main


class CliHierarchyTests(unittest.TestCase):
    def test_all_domains_require_a_domain_specific_subcommand(self):
        parser = build_parser()
        commands = [
            ["catalogue", "categories"],
            ["catalogue", "discover", "--category", "INTR"],
            ["datasets", "fetch"],
            ["validation", "run"],
            ["reports", "show"],
            ["query", "sparql", "--query", "ASK {}"],
            ["analysis", "quantitative"],
            ["analysis", "qualitative"],
            ["analysis", "all"],
        ]
        for command in commands:
            with self.subTest(command=command):
                args = parser.parse_args(command)
                self.assertIsNotNone(args.handler)

    def test_legacy_flat_commands_are_rejected(self):
        parser = build_parser()
        for command in ("discover", "categories", "fetch", "run", "sparql"):
            with self.subTest(command=command):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit):
                        parser.parse_args([command])

    def test_validation_short_circuit_is_opt_in(self):
        with patch("src.cli.run_validation") as run_mock:
            main(["validation", "run"])
        run_mock.assert_called_once_with(limit=None, short_circuit=False)

        with patch("src.cli.run_validation") as run_mock:
            main(["validation", "run", "--short-circuit"])
        run_mock.assert_called_once_with(limit=None, short_circuit=True)

    def test_analysis_modes_are_dispatched_through_the_same_cli(self):
        with patch("src.cli._execute_analysis") as analysis_mock:
            main(["analysis", "qualitative", "--shape-model", "dcatap301level1"])
        args = analysis_mock.call_args.args[0]
        self.assertEqual(args.analysis_mode, "qualitative")
        self.assertEqual(args.shape_model, ["dcatap301level1"])


if __name__ == "__main__":
    unittest.main()
