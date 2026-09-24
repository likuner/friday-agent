"""workspace_picker / resolve_workspace / 会话设置 schema 单测。"""

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.agent_tools import WORKSPACES_DIR, resolve_workspace
from app.schemas import PermissionModeSetRequest, WorkspaceSetRequest
from app.workspace_picker import _pick_command, validate_workspace_root


class TestSettingSchemas:
    def test_permission_mode_valid_values(self):
        for mode in ["default", "accept_edits", "explore", "dont_ask"]:
            assert PermissionModeSetRequest(mode=mode).mode == mode

    def test_permission_mode_rejects_unknown(self):
        for bad in ["bypass", "DEFAULT", "", "ask"]:
            with pytest.raises(ValidationError):
                PermissionModeSetRequest(mode=bad)

    def test_workspace_request_requires_path(self):
        with pytest.raises(ValidationError):
            WorkspaceSetRequest()  # 选定后不可变：不再支持 null 恢复默认
        assert WorkspaceSetRequest(path="/tmp").path == "/tmp"


class TestValidateWorkspaceRoot:
    def test_valid_directory(self, tmp_path):
        assert validate_workspace_root(str(tmp_path)) == tmp_path.resolve()

    def test_expands_home_tilde_prefix_is_rejected_without_match(self):
        # ~/xxx 是否合法取决于真实目录是否存在；这里只验证相对路径必拒
        assert validate_workspace_root("relative/dir") is None

    def test_empty_or_blank(self):
        assert validate_workspace_root("") is None
        assert validate_workspace_root("   ") is None

    def test_missing_path(self, tmp_path):
        assert validate_workspace_root(str(tmp_path / "nope")) is None

    def test_file_not_directory(self, tmp_path):
        target = tmp_path / "a.txt"
        target.write_text("x")
        assert validate_workspace_root(str(target)) is None

    def test_sensitive_directory_rejected(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        assert validate_workspace_root(str(git_dir)) is None
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        assert validate_workspace_root(str(ssh_dir)) is None


class TestResolveWorkspace:
    def test_custom_root_wins(self, tmp_path):
        cid = uuid4()
        assert resolve_workspace(cid, str(tmp_path)) == tmp_path.resolve()

    def test_default_is_conversation_dir(self):
        cid = uuid4()
        assert resolve_workspace(cid, None) == WORKSPACES_DIR / str(cid)

    def test_empty_custom_root_falls_back(self):
        cid = uuid4()
        assert resolve_workspace(cid, "") == WORKSPACES_DIR / str(cid)


class TestPickCommand:
    def test_no_native_picker_available(self, monkeypatch):
        import app.workspace_picker as picker

        monkeypatch.setattr(picker.shutil, "which", lambda _: None)
        assert picker._pick_command() is None
        assert asyncio.run(picker.pick_directory()) is None  # 平台不支持直接返回 None

    def test_prefers_osascript_when_present(self, monkeypatch):
        import app.workspace_picker as picker

        monkeypatch.setattr(picker.shutil, "which", lambda name: "/usr/bin/osascript" if name == "osascript" else None)
        command = _pick_command()
        assert command is not None and command[0] == "osascript"
