from scripts.check_runtime_permissions import check_runtime_permissions


def test_permission_check_flags_group_or_world_readable_secret(tmp_path):
    secret = tmp_path / '.env'
    secret.write_text('KEY=secret')
    secret.chmod(0o644)

    issues = check_runtime_permissions([secret])

    assert issues[0].path == secret
    assert issues[0].expected_mode == 0o600
    assert issues[0].actual_mode == 0o644


def test_permission_check_accepts_private_data_directory_and_database(tmp_path):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    database = data / 'cashier.sqlite3'
    database.touch(mode=0o600)

    assert check_runtime_permissions([data, database]) == []
