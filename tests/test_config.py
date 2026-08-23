from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web_research.config import Settings


class SavedConfigurationTests(unittest.TestCase):
    def test_reads_evidence_model_saved_by_terminal_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "evidence_model": {
                            "base_url": "http://reader.test/v1",
                            "model_id": "reader-model",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"WEB_SEARCH_DATA_DIR": directory}, clear=True):
                settings = Settings.from_env(env_file=data_dir / "missing.env")

        self.assertEqual(settings.evidence_model_base_url, "http://reader.test/v1")
        self.assertEqual(settings.evidence_model_id, "reader-model")
        self.assertEqual(settings.evidence_model_max_tokens, 1600)

    def test_nonempty_environment_values_override_saved_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "evidence_model": {
                            "base_url": "http://saved.test/v1",
                            "model_id": "saved-model",
                        }
                    }
                ),
                encoding="utf-8",
            )
            environment = {
                "WEB_SEARCH_DATA_DIR": directory,
                "WEB_SEARCH_EVIDENCE_MODEL_BASE_URL": "http://override.test/v1",
                "WEB_SEARCH_EVIDENCE_MODEL_ID": "override-model",
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env(env_file=data_dir / "missing.env")

        self.assertEqual(settings.evidence_model_base_url, "http://override.test/v1")
        self.assertEqual(settings.evidence_model_id, "override-model")

    def test_blank_environment_values_do_not_hide_saved_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "evidence_model": {
                            "base_url": "http://saved.test/v1",
                            "model_id": "saved-model",
                        }
                    }
                ),
                encoding="utf-8",
            )
            environment = {
                "WEB_SEARCH_DATA_DIR": directory,
                "WEB_SEARCH_EVIDENCE_MODEL_BASE_URL": "",
                "WEB_SEARCH_EVIDENCE_MODEL_ID": "",
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env(env_file=data_dir / "missing.env")

        self.assertEqual(settings.evidence_model_base_url, "http://saved.test/v1")
        self.assertEqual(settings.evidence_model_id, "saved-model")

    def test_project_env_supplies_api_key_to_saved_evidence_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            env_file = data_dir / ".env"
            env_file.write_text(
                "WEB_SEARCH_MODEL_API_KEY=file-secret\n"
                "WEB_SEARCH_MODEL_BASE_URL=http://model.test/v1\n",
                encoding="utf-8",
            )
            (data_dir / "config.json").write_text(
                json.dumps(
                    {
                        "evidence_model": {
                            "base_url": "http://reader.test/v1",
                            "model_id": "reader-model",
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"WEB_SEARCH_DATA_DIR": directory}, clear=True):
                settings = Settings.from_env(env_file=env_file)

        self.assertEqual(settings.model_api_key, "file-secret")
        self.assertEqual(settings.evidence_model_api_key, "file-secret")

    def test_process_environment_overrides_project_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "WEB_SEARCH_MODEL_API_KEY=file-secret\nWEB_SEARCH_LOG_LEVEL=DEBUG\n",
                encoding="utf-8",
            )
            environment = {
                "WEB_SEARCH_DATA_DIR": directory,
                "WEB_SEARCH_MODEL_API_KEY": "process-secret",
                "WEB_SEARCH_LOG_LEVEL": "WARNING",
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env(env_file=env_file)

        self.assertEqual(settings.model_api_key, "process-secret")
        self.assertEqual(settings.evidence_model_api_key, "process-secret")
        self.assertEqual(settings.log_level, "WARNING")


if __name__ == "__main__":
    unittest.main()
