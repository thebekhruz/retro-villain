#!/usr/bin/env bash
set -u

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
TEST_TMP=$(mktemp -d)
trap 'rm -rf "$TEST_TMP"' EXIT

passed=0
failed=0
stdout=""
stderr=""
status=0

run_cli() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  : >"$out_file"
  : >"$err_file"
  env -u IIKO_API_LOGIN -u IIKO_ORGANIZATION_ID \
    IIKO_ENV_FILE="$TEST_TMP/missing.env" \
    bash "$ROOT_DIR/iiko.sh" "$@" >"$out_file" 2>"$err_file"
  status=$?
  stdout=$(<"$out_file")
  stderr=$(<"$err_file")
}

run_cli_with_api() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  : >"$out_file"
  : >"$err_file"
  : >"$TEST_TMP/curl.log"
  IIKO_ENV_FILE="$TEST_TMP/missing.env" \
    IIKO_API_LOGIN=test-login \
    IIKO_CURL_BIN="$ROOT_DIR/tests/fake-curl.sh" \
    IIKO_FAKE_CURL_LOG="$TEST_TMP/curl.log" \
    bash "$ROOT_DIR/iiko.sh" "$@" >"$out_file" 2>"$err_file"
  status=$?
  stdout=$(<"$out_file")
  stderr=$(<"$err_file")
}

run_cli_with_web() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  : >"$out_file"
  : >"$err_file"
  : >"$TEST_TMP/curl.log"
  env -u IIKO_API_LOGIN \
    IIKO_ENV_FILE="$TEST_TMP/missing.env" \
    IIKO_MODE=web \
    IIKO_SERVER_URL=https://fixture.iikoweb.test \
    IIKO_LOGIN=fixture-user \
    IIKO_PASSWORD=fixture-password \
    IIKO_STORE_ID=82907 \
    IIKO_CURL_BIN="$ROOT_DIR/tests/fake-curl.sh" \
    IIKO_FAKE_CURL_LOG="$TEST_TMP/curl.log" \
    bash "$ROOT_DIR/iiko.sh" "$@" >"$out_file" 2>"$err_file"
  status=$?
  stdout=$(<"$out_file")
  stderr=$(<"$err_file")
}

assert_eq() {
  local expected=$1 actual=$2 label=$3
  if [[ "$actual" == "$expected" ]]; then
    passed=$((passed + 1))
  else
    printf 'FAIL: %s\n  expected: %q\n  actual:   %q\n' "$label" "$expected" "$actual" >&2
    failed=$((failed + 1))
  fi
}

assert_contains() {
  local haystack=$1 needle=$2 label=$3
  if [[ "$haystack" == *"$needle"* ]]; then
    passed=$((passed + 1))
  else
    printf 'FAIL: %s\n  missing: %q\n  in:      %q\n' "$label" "$needle" "$haystack" >&2
    failed=$((failed + 1))
  fi
}

assert_not_contains() {
  local haystack=$1 needle=$2 label=$3
  if [[ "$haystack" != *"$needle"* ]]; then
    passed=$((passed + 1))
  else
    printf 'FAIL: %s\n  unexpected: %q\n  in:         %q\n' "$label" "$needle" "$haystack" >&2
    failed=$((failed + 1))
  fi
}

test_help_lists_commands() {
  run_cli --help
  assert_eq 0 "$status" "help exits successfully"
  assert_contains "$stdout" "yandex-menu" "help lists yandex-menu"
  assert_contains "$stdout" "organizations" "help lists organizations"
}

test_doctor_requires_api_login() {
  run_cli doctor
  assert_eq 2 "$status" "doctor rejects missing API login"
  assert_contains "$stderr" "IIKO_API_LOGIN" "missing-login error names variable"
}

test_doctor_rejects_invalid_timeout() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  IIKO_ENV_FILE="$TEST_TMP/missing.env" IIKO_API_LOGIN=test-login IIKO_TIMEOUT=abc \
    bash "$ROOT_DIR/iiko.sh" doctor >"$out_file" 2>"$err_file"
  status=$?
  stderr=$(<"$err_file")
  assert_eq 2 "$status" "doctor rejects invalid timeout"
  assert_contains "$stderr" "IIKO_TIMEOUT" "timeout error names variable"
}

test_environment_beats_dotenv() {
  printf 'IIKO_API_LOGIN=file-login\nIIKO_TIMEOUT=abc\n' >"$TEST_TMP/test.env"
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  IIKO_ENV_FILE="$TEST_TMP/test.env" IIKO_API_LOGIN=environment-login IIKO_TIMEOUT=30 \
    IIKO_SKIP_LIVE_DOCTOR=1 bash "$ROOT_DIR/iiko.sh" doctor >"$out_file" 2>"$err_file"
  status=$?
  stdout=$(<"$out_file")
  assert_eq 0 "$status" "environment configuration takes precedence"
  assert_contains "$stdout" "Local configuration: OK" "doctor reports local success"
}

test_organizations_returns_valid_json_without_token_leak() {
  run_cli_with_api organizations --json
  assert_eq 0 "$status" "organizations exits successfully"
  if jq -e '.organizations | length == 2' <<<"$stdout" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: organizations returns two-item JSON\n' >&2
    failed=$((failed + 1))
  fi
  local log
  log=$(<"$TEST_TMP/curl.log")
  assert_contains "$log" "/access_token" "authentication endpoint called"
  assert_contains "$log" "/organizations" "organizations endpoint called"
  assert_not_contains "$stdout$stderr" "fixture-token" "token is not printed"
}

test_organizations_human_output_is_tabular() {
  run_cli_with_api organizations
  assert_eq 0 "$status" "human organization list succeeds"
  assert_contains "$stdout" "11111111-1111-1111-1111-111111111111" "organization ID is shown"
  assert_contains "$stdout" "Ретро" "organization name is shown"
}

test_http_allowlist_rejects_mutation_endpoint() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  : >"$TEST_TMP/curl.log"
  IIKO_ENV_FILE="$TEST_TMP/missing.env" IIKO_API_LOGIN=test-login \
    IIKO_CURL_BIN="$ROOT_DIR/tests/fake-curl.sh" IIKO_FAKE_CURL_LOG="$TEST_TMP/curl.log" \
    bash -c 'source "$1"; load_env; apply_defaults; api_post /deliveries/create "{}" none' _ \
    "$ROOT_DIR/iiko.sh" >"$out_file" 2>"$err_file"
  status=$?
  stderr=$(<"$err_file")
  assert_eq 70 "$status" "mutation endpoint is rejected"
  assert_contains "$stderr" "not allowlisted" "allowlist error is explicit"
  assert_eq "" "$(<"$TEST_TMP/curl.log")" "curl is not called for rejected endpoint"
}

test_nomenclature_uses_explicit_organization() {
  run_cli_with_api nomenclature --organization 11111111-1111-1111-1111-111111111111
  assert_eq 0 "$status" "nomenclature succeeds for explicit organization"
  if jq -e '.products | length == 3' <<<"$stdout" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: nomenclature returns fixture products\n' >&2
    failed=$((failed + 1))
  fi
  local log
  log=$(<"$TEST_TMP/curl.log")
  assert_contains "$log" '"organizationId":"11111111-1111-1111-1111-111111111111"' "nomenclature body contains organization"
  assert_contains "$log" '"startRevision":0' "nomenclature requests full revision"
}

test_nomenclature_requires_choice_for_multiple_organizations() {
  run_cli_with_api nomenclature
  assert_eq 2 "$status" "multiple organizations require a choice"
  assert_contains "$stderr" "IIKO_ORGANIZATION_ID" "selection error explains configuration"
  assert_contains "$stderr" "Ретро" "selection error lists organizations"
}

test_nomenclature_uses_configured_organization() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  : >"$TEST_TMP/curl.log"
  IIKO_ENV_FILE="$TEST_TMP/missing.env" IIKO_API_LOGIN=test-login \
    IIKO_ORGANIZATION_ID=22222222-2222-2222-2222-222222222222 \
    IIKO_CURL_BIN="$ROOT_DIR/tests/fake-curl.sh" IIKO_FAKE_CURL_LOG="$TEST_TMP/curl.log" \
    bash "$ROOT_DIR/iiko.sh" nomenclature >"$out_file" 2>"$err_file"
  status=$?
  assert_eq 0 "$status" "configured organization is accepted"
  assert_contains "$(<"$TEST_TMP/curl.log")" '"organizationId":"22222222-2222-2222-2222-222222222222"' "configured organization is requested"
}

test_nomenclature_auto_selects_only_organization() {
  local out_file="$TEST_TMP/stdout"
  local err_file="$TEST_TMP/stderr"
  : >"$TEST_TMP/curl.log"
  IIKO_ENV_FILE="$TEST_TMP/missing.env" IIKO_API_LOGIN=test-login \
    IIKO_CURL_BIN="$ROOT_DIR/tests/fake-curl.sh" IIKO_FAKE_CURL_LOG="$TEST_TMP/curl.log" \
    IIKO_FAKE_ORGANIZATIONS_FIXTURE="$ROOT_DIR/tests/fixtures/organizations_one.json" \
    bash "$ROOT_DIR/iiko.sh" nomenclature >"$out_file" 2>"$err_file"
  status=$?
  assert_eq 0 "$status" "sole organization is selected automatically"
  assert_contains "$(<"$TEST_TMP/curl.log")" '"organizationId":"11111111-1111-1111-1111-111111111111"' "sole organization is requested"
}

test_nomenclature_writes_valid_output_file() {
  local destination="$TEST_TMP/nomenclature.json"
  run_cli_with_api nomenclature --organization 11111111-1111-1111-1111-111111111111 --output "$destination"
  assert_eq 0 "$status" "nomenclature file export succeeds"
  if jq -e '.products[1].id == "product-yandex"' "$destination" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: nomenclature output file contains valid fixture data\n' >&2
    failed=$((failed + 1))
  fi
}

test_yandex_menu_filters_and_normalizes_json() {
  run_cli_with_api yandex-menu --organization 11111111-1111-1111-1111-111111111111 --format json
  assert_eq 0 "$status" "Yandex JSON export succeeds"
  if jq -e '
    length == 2 and
    all(.[]; (.name | ascii_downcase | contains("yandex")) or (.groupName | contains("ЯНДЕКС"))) and
    any(.[]; .productId == "product-yandex" and .price == 40000 and .isDeleted == false) and
    any(.[]; .productId == "product-deleted" and .price == null and .isDeleted == true) and
    all(.[]; .productId != "product-normal")
  ' <<<"$stdout" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: Yandex JSON has normalized active and deleted products\n%s\n' "$stdout" >&2
    failed=$((failed + 1))
  fi
}

test_yandex_menu_exports_stable_csv() {
  run_cli_with_api yandex-menu --organization 11111111-1111-1111-1111-111111111111 --format csv
  assert_eq 0 "$status" "Yandex CSV export succeeds"
  local header line_count
  header=${stdout%%$'\n'*}
  line_count=$(printf '%s\n' "$stdout" | wc -l | tr -d ' ')
  assert_eq '"organization_id","group_id","group_name","product_id","article","name","type","measure_unit","category_id","category_name","price","is_deleted"' "$header" "CSV header is stable"
  assert_eq 3 "$line_count" "CSV contains header and two Yandex products"
  assert_contains "$stdout" '"02097"' "CSV contains active article"
  assert_contains "$stdout" '"99999"' "CSV contains deleted article"
}

test_yandex_menu_rejects_unknown_format() {
  run_cli_with_api yandex-menu --organization 11111111-1111-1111-1111-111111111111 --format yaml
  assert_eq 2 "$status" "unknown export format is rejected"
  assert_contains "$stderr" "json or csv" "format error lists supported formats"
}

test_yandex_menu_writes_csv_file() {
  local destination="$TEST_TMP/yandex.csv"
  run_cli_with_api yandex-menu --organization 11111111-1111-1111-1111-111111111111 --format csv --output "$destination"
  assert_eq 0 "$status" "CSV file export succeeds"
  assert_contains "$(<"$destination")" '"organization_id"' "CSV file has header"
  assert_contains "$(<"$destination")" '"product-yandex"' "CSV file has Yandex product"
}

test_web_doctor_uses_existing_login_configuration() {
  run_cli_with_web doctor
  assert_eq 0 "$status" "web doctor accepts login/password configuration"
  assert_contains "$stdout" "iikoWeb access: OK" "web doctor confirms access"
  assert_not_contains "$stdout$stderr" "fixture-password" "web password is never printed"
  assert_not_contains "$stdout$stderr" "web-fixture-token" "web token is never printed"
}

test_web_stores_returns_json() {
  run_cli_with_web stores --json
  assert_eq 0 "$status" "web stores command succeeds"
  if jq -e '.stores | length == 2' <<<"$stdout" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: web stores returns two stores\n' >&2
    failed=$((failed + 1))
  fi
}

test_web_revenue_returns_normalized_rows() {
  run_cli_with_web revenue --date 2026-09-13
  assert_eq 0 "$status" "web revenue command succeeds"
  if jq -e '
    .date == "2026-09-13" and .storeId == 82907 and
    (.rows | length == 2) and
    (.rows[1] == {paymentType:"Яндекс",ordersCount:2,averageCheck:75000,revenue:150000}) and
    .totalRevenue == 650000
  ' <<<"$stdout" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: web revenue returns normalized totals\n%s\n' "$stdout" >&2
    failed=$((failed + 1))
  fi
}

test_web_dish_sales_returns_normalized_rows() {
  run_cli_with_web dish-sales --from 2026-09-01 --to 2026-09-13
  assert_eq 0 "$status" "web dish-sales command succeeds"
  if jq -e '
    .dateFrom == "2026-09-01" and .dateTo == "2026-09-13" and
    (.rows | length == 2) and
    any(.rows[]; .productId == "02097" and .quantity == 3 and .revenue == 120000)
  ' <<<"$stdout" >/dev/null 2>&1; then
    passed=$((passed + 1))
  else
    printf 'FAIL: web dish-sales returns normalized dishes\n%s\n' "$stdout" >&2
    failed=$((failed + 1))
  fi
  local log
  log=$(<"$TEST_TMP/curl.log")
  assert_contains "$log" '"groupFields":["DishId","DishName","DishCategory.Accounting"]' "dish-sales uses iikoWeb field identifiers"
  assert_not_contains "$log" 'Dish.Code' "dish-sales does not use legacy dotted dish code"
}

test_web_mode_rejects_cloud_only_command() {
  run_cli_with_web nomenclature
  assert_eq 2 "$status" "web mode rejects Cloud-only nomenclature"
  assert_contains "$stderr" "requires iikoCloud" "Cloud-only error explains requirement"
}

test_help_lists_commands
test_doctor_requires_api_login
test_doctor_rejects_invalid_timeout
test_environment_beats_dotenv
test_organizations_returns_valid_json_without_token_leak
test_organizations_human_output_is_tabular
test_http_allowlist_rejects_mutation_endpoint
test_nomenclature_uses_explicit_organization
test_nomenclature_requires_choice_for_multiple_organizations
test_nomenclature_uses_configured_organization
test_nomenclature_auto_selects_only_organization
test_nomenclature_writes_valid_output_file
test_yandex_menu_filters_and_normalizes_json
test_yandex_menu_exports_stable_csv
test_yandex_menu_rejects_unknown_format
test_yandex_menu_writes_csv_file
test_web_doctor_uses_existing_login_configuration
test_web_stores_returns_json
test_web_revenue_returns_normalized_rows
test_web_dish_sales_returns_normalized_rows
test_web_mode_rejects_cloud_only_command

printf '%s passed, %s failed\n' "$passed" "$failed"
((failed == 0))
