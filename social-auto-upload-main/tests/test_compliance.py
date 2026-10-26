from __future__ import annotations

import pytest

import compliance
import publish_jobs


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    video_dir = tmp_path / "videoFile"
    video_dir.mkdir()
    (video_dir / "clip.mp4").write_bytes(b"video")
    (video_dir / "cover.png").write_bytes(b"image")
    monkeypatch.setattr(compliance, "BASE_DIR", tmp_path)
    monkeypatch.setattr(publish_jobs, "DB", tmp_path / "publish_jobs.db")
    publish_jobs.init_db()
    return tmp_path


def base_payload(**overrides):
    data = {
        "type": 1,
        "title": "周末带娃去哪玩",
        "tags": ["亲子游"],
        "fileList": ["clip.mp4"],
    }
    data.update(overrides)
    return data


def test_valid_content_passes(workdir):
    result = compliance.check_content(base_payload())
    assert result["passed"] is True
    assert result["errors"] == []


def test_empty_title_and_missing_file_fail(workdir):
    result = compliance.check_content(base_payload(title="", fileList=[]))
    assert result["passed"] is False
    assert "标题不能为空" in result["errors"]
    assert "没有选择素材" in result["errors"]


def test_title_length_respects_platform_limit(workdir):
    result = compliance.check_content(base_payload(title="超" * 31))
    assert result["passed"] is False
    assert any("超过小红书上限" in item for item in result["errors"])


def test_absolute_advertising_word_is_rejected(workdir):
    result = compliance.check_content(base_payload(title="全网第一的亲子路线"))
    assert result["passed"] is False
    assert any("绝对化" in item for item in result["errors"])


def test_phone_number_is_rejected(workdir):
    result = compliance.check_content(base_payload(title="咨询 13800138000"))
    assert result["passed"] is False
    assert any("手机号" in item for item in result["errors"])


def test_wechat_contact_is_rejected(workdir):
    result = compliance.check_content(base_payload(title="加微信 abcdefg 领资料"))
    assert result["passed"] is False
    assert any("联系方式" in item for item in result["errors"])


def test_external_link_is_warning_only(workdir):
    result = compliance.check_content(base_payload(title="行程详情 https://example.com"))
    assert result["passed"] is True
    assert any("外部链接" in item for item in result["warnings"])


def test_too_many_tags_is_warning(workdir):
    result = compliance.check_content(base_payload(tags=[f"t{i}" for i in range(11)]))
    assert result["passed"] is True
    assert any("话题 11 个" in item for item in result["warnings"])


def test_missing_material_file_is_error(workdir):
    result = compliance.check_content(base_payload(fileList=["missing.mp4"]))
    assert result["passed"] is False
    assert any("素材文件不存在" in item for item in result["errors"])


def test_video_platform_without_cover_warns(workdir):
    result = compliance.check_content(base_payload(type=5, fileList=["clip.mp4"]))
    assert result["passed"] is True
    assert any("发布需要封面" in item for item in result["warnings"])


def test_duplicate_title_warns(workdir):
    with publish_jobs.get_conn() as conn:
        conn.execute(
            "INSERT INTO publish_records (type,title,status) VALUES (?,?,?)",
            (1, "周末带娃去哪玩", "success"),
        )
    result = compliance.check_content(base_payload())
    assert result["passed"] is True
    assert any("同名标题" in item for item in result["warnings"])
