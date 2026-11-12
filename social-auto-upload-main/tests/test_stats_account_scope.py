from stats_reflow import (
    _record_accounts,
    filter_current_account_records,
    filter_current_account_jobs,
    filter_current_account_rows,
)


ACCOUNTS = [
    {
        "id": 6,
        "type": 3,
        "userName": "龙脊",
        "filePath": "douyin_current.json",
    },
]


def test_explicit_orphaned_record_does_not_fall_back_to_current_account():
    record = {"type": 3, "accounts": "douyin_deleted.json"}

    assert _record_accounts(record, ACCOUNTS) == []


def test_record_without_account_metadata_can_use_current_platform_account():
    record = {"type": 3, "accounts": ""}

    assert _record_accounts(record, ACCOUNTS) == ACCOUNTS


def test_filter_records_hides_old_platform_accounts():
    records = [
        {"id": 1, "type": 3, "accounts": "douyin_current.json"},
        {"id": 2, "type": 3, "accounts": "douyin_deleted.json"},
        {"id": 3, "type": 4, "accounts": "kuaishou_deleted.json"},
    ]

    assert [row["id"] for row in filter_current_account_records(records, ACCOUNTS)] == [1]


def test_filter_stats_keeps_only_existing_accounts():
    rows = [
        {"id": 1, "platform": 3, "account": "龙脊"},
        {"id": 2, "platform": 3, "account": "陆大头"},
        {"id": 3, "platform": 4, "account": "测试22222"},
        {"id": 4, "platform": 1, "account": "大头"},
    ]

    assert [row["id"] for row in filter_current_account_rows(rows, ACCOUNTS)] == [1]


def test_filter_jobs_hides_removed_account_lists_and_schedule_entries():
    jobs = [
        {"id": 1, "type": 3, "account_list": '["douyin_current.json"]'},
        {"id": 2, "type": 3, "account_list": '["douyin_deleted.json"]'},
        {"id": 3, "type": 4, "account": "kuaishou_deleted.json"},
        {"id": 4, "type": 1, "account": "douyin_current.json"},
    ]

    assert [row["id"] for row in filter_current_account_jobs(jobs, ACCOUNTS)] == [1]


def test_expired_accounts_are_hidden_everywhere():
    """失效账号（status=0）的数据要全局隐藏：周报/发布记录/统计都不展示。"""
    accounts = ACCOUNTS + [
        {
            "id": 7,
            "type": 3,
            "userName": "已失效号",
            "filePath": "douyin_expired.json",
            "status": 0,
        },
    ]
    # 发布记录：失效账号的不展示
    records = [
        {"id": 1, "type": 3, "accounts": "douyin_current.json"},
        {"id": 2, "type": 3, "accounts": "douyin_expired.json"},
    ]
    assert [r["id"] for r in filter_current_account_records(records, accounts)] == [1]
    # 内容统计：失效账号的不展示
    rows = [
        {"id": 1, "platform": 3, "account": "龙脊"},
        {"id": 2, "platform": 3, "account": "已失效号"},
        {"id": 3, "platform": 3, "account": "douyin_expired.json"},
    ]
    assert [r["id"] for r in filter_current_account_rows(rows, accounts)] == [1]
    # 任务队列：失效账号的不展示
    jobs = [
        {"id": 1, "type": 3, "account_list": '["douyin_current.json"]'},
        {"id": 2, "type": 3, "account_list": '["douyin_expired.json"]'},
    ]
    assert [j["id"] for j in filter_current_account_jobs(jobs, accounts)] == [1]
