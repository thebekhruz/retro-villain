#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
WEB_TOKEN=""
WEB_COOKIE_HEADER=""
WEB_COOKIE_JAR=""

die() {
  local code=$1
  shift
  printf 'Error: %s\n' "$*" >&2
  exit "$code"
}

load_env() {
  local env_file=${IIKO_ENV_FILE:-"$SCRIPT_DIR/.env"}
  [[ -f "$env_file" ]] || return 0

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key=${BASH_REMATCH[1]}
    value=${BASH_REMATCH[2]}

    if [[ ${#value} -ge 2 ]]; then
      if [[ ${value:0:1} == '"' && ${value: -1} == '"' ]] ||
         [[ ${value:0:1} == "'" && ${value: -1} == "'" ]]; then
        value=${value:1:${#value}-2}
      fi
    fi

    if ! printenv "$key" >/dev/null 2>&1; then
      printf -v "$key" '%s' "$value"
      export "$key"
    fi
  done <"$env_file"
}

apply_defaults() {
  IIKO_API_BASE_URL=${IIKO_API_BASE_URL:-https://api-ru.iiko.services/api/1}
  IIKO_TIMEOUT=${IIKO_TIMEOUT:-30}
  IIKO_CURL_BIN=${IIKO_CURL_BIN:-curl}
  IIKO_MODE=${IIKO_MODE:-auto}
  IIKO_SERVER_URL=${IIKO_SERVER_URL:-${IIKO_BASE_URL:-}}
  IIKO_LOGIN=${IIKO_LOGIN:-${IIKO_USERNAME:-}}
  IIKO_PASSWORD=${IIKO_PASSWORD:-}
  IIKO_STORE_ID=${IIKO_STORE_ID:-}
  IIKO_COOKIE=${IIKO_COOKIE:-}
  IIKO_GROUPED_RETRY_COUNT=${IIKO_GROUPED_RETRY_COUNT:-15}
  IIKO_GROUPED_RETRY_DELAY_MS=${IIKO_GROUPED_RETRY_DELAY_MS:-1000}
}

require_dependencies() {
  command -v jq >/dev/null 2>&1 || die 2 "Required command not found: jq"
  command -v mktemp >/dev/null 2>&1 || die 2 "Required command not found: mktemp"
  command -v "$IIKO_CURL_BIN" >/dev/null 2>&1 || die 2 "Required command not found: $IIKO_CURL_BIN"
}

validate_common_config() {
  [[ "$IIKO_TIMEOUT" =~ ^[0-9]+$ ]] || die 2 "IIKO_TIMEOUT must be an integer from 1 to 300."
  ((IIKO_TIMEOUT >= 1 && IIKO_TIMEOUT <= 300)) || die 2 "IIKO_TIMEOUT must be an integer from 1 to 300."
  [[ "$IIKO_MODE" == auto || "$IIKO_MODE" == cloud || "$IIKO_MODE" == web ]] ||
    die 2 "IIKO_MODE must be auto, cloud, or web."
}

validate_cloud_config() {
  validate_common_config
  [[ -n ${IIKO_API_LOGIN:-} ]] || die 2 "This command requires iikoCloud: IIKO_API_LOGIN is required."
  [[ "$IIKO_API_BASE_URL" =~ ^https://[^[:space:]]+$ ]] || die 2 "IIKO_API_BASE_URL must use HTTPS."
}

validate_web_config() {
  validate_common_config
  [[ "$IIKO_SERVER_URL" =~ ^https://[^[:space:]]+$ ]] ||
    die 2 "IIKO_SERVER_URL (or IIKO_BASE_URL) must be an HTTPS iikoWeb address."
  [[ "$IIKO_STORE_ID" =~ ^[0-9]+$ ]] || die 2 "IIKO_STORE_ID must be a numeric store ID."
  if [[ -z "$IIKO_COOKIE" && ( -z "$IIKO_LOGIN" || -z "$IIKO_PASSWORD" ) ]]; then
    die 2 "Set IIKO_COOKIE or both IIKO_LOGIN/IIKO_USERNAME and IIKO_PASSWORD."
  fi
  [[ "$IIKO_GROUPED_RETRY_COUNT" =~ ^[0-9]+$ ]] && ((IIKO_GROUPED_RETRY_COUNT >= 1 && IIKO_GROUPED_RETRY_COUNT <= 60)) ||
    die 2 "IIKO_GROUPED_RETRY_COUNT must be an integer from 1 to 60."
  [[ "$IIKO_GROUPED_RETRY_DELAY_MS" =~ ^[0-9]+$ ]] ||
    die 2 "IIKO_GROUPED_RETRY_DELAY_MS must be a non-negative integer."
}

validate_config() {
  validate_cloud_config
}

detect_mode() {
  if [[ "$IIKO_MODE" != auto ]]; then
    printf '%s' "$IIKO_MODE"
  elif [[ -n "$IIKO_SERVER_URL" && -n "$IIKO_STORE_ID" && ( -n "$IIKO_COOKIE" || ( -n "$IIKO_LOGIN" && -n "$IIKO_PASSWORD" ) ) ]]; then
    printf 'web'
  else
    printf 'cloud'
  fi
}

usage() {
  cat <<'EOF'
Usage: ./iiko.sh COMMAND [OPTIONS]

Read-only commands:
  doctor          Check dependencies, configuration, and API access
  organizations   List organizations available to the API login
  nomenclature    Download organization nomenclature as JSON
  yandex-menu     Export Yandex delivery products as JSON or CSV
  stores           List stores available through iikoWeb login
  revenue          Read revenue by payment type from iikoWeb OLAP
  dish-sales       Read dish quantities and revenue from iikoWeb OLAP

Run ./iiko.sh COMMAND --help for command-specific options.
EOF
}

api_post() {
  local path=$1
  local body_json=$2
  local token=${3:-}

  case "$path" in
    /access_token|/organizations|/nomenclature) ;;
    *) die 70 "Endpoint is not allowlisted: $path" ;;
  esac

  jq -e . <<<"$body_json" >/dev/null 2>&1 || die 70 "Internal error: request body is not valid JSON."

  local response_file http_status
  response_file=$(mktemp "${TMPDIR:-/tmp}/iiko-response.XXXXXX") || die 74 "Could not create a temporary response file."
  chmod 600 "$response_file"

  local -a curl_args=(
    --silent --show-error
    --request POST
    --connect-timeout "$IIKO_TIMEOUT"
    --max-time "$IIKO_TIMEOUT"
    --header 'Content-Type: application/json'
    --data-binary "$body_json"
    --output "$response_file"
    --write-out '%{http_code}'
  )
  if [[ -n "$token" ]]; then
    curl_args+=(--header "Authorization: Bearer $token")
  fi

  set +e
  http_status=$("$IIKO_CURL_BIN" "${curl_args[@]}" "${IIKO_API_BASE_URL%/}$path")
  local curl_status=$?
  set -e

  if ((curl_status != 0)); then
    rm -f "$response_file"
    die 69 "iikoCloud request failed before receiving a response."
  fi
  if ! jq -e . "$response_file" >/dev/null 2>&1; then
    rm -f "$response_file"
    die 69 "iikoCloud returned invalid JSON (HTTP $http_status)."
  fi
  if [[ ! "$http_status" =~ ^2[0-9][0-9]$ ]]; then
    local description
    description=$(jq -r '.errorDescription // .error // .message // "iikoCloud request failed"' "$response_file")
    rm -f "$response_file"
    die 69 "$description (HTTP $http_status)."
  fi

  cat "$response_file"
  rm -f "$response_file"
}

get_access_token() {
  local payload response token
  payload=$(jq -nc --arg apiLogin "$IIKO_API_LOGIN" '{apiLogin:$apiLogin}')
  response=$(api_post /access_token "$payload")
  token=$(jq -er '.token | select(type == "string" and length > 0)' <<<"$response") ||
    die 69 "iikoCloud authentication response did not contain a token."
  printf '%s' "$token"
}

get_organizations() {
  local token
  token=$(get_access_token)
  api_post /organizations '{}' "$token"
}

organizations_command() {
  local format=table
  while (($#)); do
    case "$1" in
      --json) format=json ;;
      -h|--help)
        printf 'Usage: ./iiko.sh organizations [--json]\n'
        return 0
        ;;
      *) die 2 "Unknown organizations option: $1" ;;
    esac
    shift
  done

  require_dependencies
  validate_config
  local response
  response=$(get_organizations)
  jq -e '.organizations | type == "array"' <<<"$response" >/dev/null ||
    die 69 "iikoCloud response does not contain an organizations array."
  if [[ "$format" == json ]]; then
    jq . <<<"$response"
  else
    printf 'ID\tNAME\n'
    jq -r '.organizations[] | [.id, .name] | @tsv' <<<"$response"
  fi
}

validate_uuid() {
  local value=$1
  [[ "$value" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]
}

resolve_organization_id() {
  local explicit_id=${1:-}
  local selected_id=${explicit_id:-${IIKO_ORGANIZATION_ID:-}}
  local organizations count
  organizations=$(get_organizations)
  count=$(jq -r '.organizations | length' <<<"$organizations")

  if [[ -z "$selected_id" ]]; then
    if [[ "$count" == 1 ]]; then
      selected_id=$(jq -r '.organizations[0].id' <<<"$organizations")
      printf 'Using the only available organization: %s\n' \
        "$(jq -r '.organizations[0].name' <<<"$organizations")" >&2
    else
      printf 'Available organizations:\n' >&2
      jq -r '.organizations[] | "  \(.id)\t\(.name)"' <<<"$organizations" >&2
      die 2 "Select one with --organization UUID or IIKO_ORGANIZATION_ID."
    fi
  fi

  validate_uuid "$selected_id" || die 2 "Organization ID is not a valid UUID: $selected_id"
  jq -e --arg id "$selected_id" '.organizations | any(.id == $id)' <<<"$organizations" >/dev/null ||
    die 2 "Organization is not available to this API login: $selected_id"
  printf '%s' "$selected_id"
}

get_nomenclature() {
  local organization_id=$1
  local token payload
  token=$(get_access_token)
  payload=$(jq -nc --arg organizationId "$organization_id" \
    '{organizationId:$organizationId,startRevision:0}')
  api_post /nomenclature "$payload" "$token"
}

write_json_output() {
  local json=$1
  local output=${2:-}
  if [[ -z "$output" ]]; then
    jq . <<<"$json"
    return 0
  fi

  local destination_dir temp_file
  destination_dir=$(dirname "$output")
  [[ -d "$destination_dir" ]] || die 74 "Output directory does not exist: $destination_dir"
  temp_file=$(mktemp "$destination_dir/.iiko-export.XXXXXX") || die 74 "Could not create an export file."
  chmod 600 "$temp_file"
  if ! jq . <<<"$json" >"$temp_file"; then
    rm -f "$temp_file"
    die 74 "Could not write valid JSON export."
  fi
  mv "$temp_file" "$output"
  printf 'Saved: %s\n' "$output" >&2
}

write_text_output() {
  local content=$1
  local output=${2:-}
  if [[ -z "$output" ]]; then
    printf '%s\n' "$content"
    return 0
  fi

  local destination_dir temp_file
  destination_dir=$(dirname "$output")
  [[ -d "$destination_dir" ]] || die 74 "Output directory does not exist: $destination_dir"
  temp_file=$(mktemp "$destination_dir/.iiko-export.XXXXXX") || die 74 "Could not create an export file."
  chmod 600 "$temp_file"
  printf '%s\n' "$content" >"$temp_file" || {
    rm -f "$temp_file"
    die 74 "Could not write export file."
  }
  mv "$temp_file" "$output"
  printf 'Saved: %s\n' "$output" >&2
}

nomenclature_command() {
  local explicit_id="" output=""
  while (($#)); do
    case "$1" in
      --organization)
        (($# >= 2)) || die 2 "--organization requires a UUID."
        explicit_id=$2
        shift 2
        ;;
      --output)
        (($# >= 2)) || die 2 "--output requires a file path."
        output=$2
        shift 2
        ;;
      -h|--help)
        printf 'Usage: ./iiko.sh nomenclature [--organization UUID] [--output FILE]\n'
        return 0
        ;;
      *) die 2 "Unknown nomenclature option: $1" ;;
    esac
  done

  require_dependencies
  validate_config
  local organization_id response
  organization_id=$(resolve_organization_id "$explicit_id")
  response=$(get_nomenclature "$organization_id")
  jq -e '.products | type == "array"' <<<"$response" >/dev/null ||
    die 69 "iikoCloud response does not contain a products array."
  write_json_output "$response" "$output"
}

filter_yandex_menu() {
  local organization_id=$1
  jq --arg organizationId "$organization_id" '
    (.groups // [] | map({key: .id, value: .}) | from_entries) as $groups |
    (.productCategories // [] | map({key: .id, value: .}) | from_entries) as $categories |
    [
      (.products // [])[] |
      . as $product |
      ($groups[$product.parentGroup] // {}) as $group |
      ($categories[$product.productCategoryId] // {}) as $category |
      select(((($group.name // "") + " " + ($product.name // "")) | test("яндекс|yandex"; "i"))) |
      {
        organizationId: $organizationId,
        groupId: ($product.parentGroup // null),
        groupName: ($group.name // null),
        productId: ($product.id // null),
        article: ($product.code // null),
        name: ($product.name // null),
        type: ($product.type // null),
        measureUnit: ($product.measureUnit // null),
        categoryId: ($product.productCategoryId // null),
        categoryName: ($category.name // null),
        price: ([ $product.sizePrices[]?.price.currentPrice // empty ] | first // null),
        isDeleted: ($product.isDeleted // false)
      }
    ] | sort_by(.name, .article)
  '
}

yandex_to_csv() {
  jq -r '
    (["organization_id", "group_id", "group_name", "product_id", "article", "name", "type", "measure_unit", "category_id", "category_name", "price", "is_deleted"] | @csv),
    (.[] | [
      .organizationId, .groupId, .groupName, .productId, .article, .name,
      .type, .measureUnit, .categoryId, .categoryName, .price, .isDeleted
    ] | @csv)
  '
}

yandex_menu_command() {
  local explicit_id="" output="" format=json
  while (($#)); do
    case "$1" in
      --organization)
        (($# >= 2)) || die 2 "--organization requires a UUID."
        explicit_id=$2
        shift 2
        ;;
      --format)
        (($# >= 2)) || die 2 "--format requires json or csv."
        format=$2
        shift 2
        ;;
      --output)
        (($# >= 2)) || die 2 "--output requires a file path."
        output=$2
        shift 2
        ;;
      -h|--help)
        printf 'Usage: ./iiko.sh yandex-menu [--organization UUID] [--format json|csv] [--output FILE]\n'
        return 0
        ;;
      *) die 2 "Unknown yandex-menu option: $1" ;;
    esac
  done
  [[ "$format" == json || "$format" == csv ]] || die 2 "--format must be json or csv."

  require_dependencies
  validate_config
  local organization_id nomenclature normalized rendered
  organization_id=$(resolve_organization_id "$explicit_id")
  nomenclature=$(get_nomenclature "$organization_id")
  normalized=$(filter_yandex_menu "$organization_id" <<<"$nomenclature")

  if [[ "$format" == json ]]; then
    write_json_output "$normalized" "$output"
  else
    rendered=$(yandex_to_csv <<<"$normalized")
    write_text_output "$rendered" "$output"
  fi
}

cleanup_web_session() {
  if [[ -n "$WEB_COOKIE_JAR" && -f "$WEB_COOKIE_JAR" ]]; then
    rm -f "$WEB_COOKIE_JAR"
  fi
}

web_request() {
  local method=$1 path=$2 body_json=${3:-}
  case "$method:$path" in
    POST:/api/auth/login|GET:/api/auth|GET:/api/stores/list|POST:/api/olap/init|POST:/api/olap/fetch/*/grouped-table) ;;
    *) die 70 "iikoWeb endpoint is not allowlisted: $method $path" ;;
  esac

  if [[ -n "$body_json" ]]; then
    jq -e . <<<"$body_json" >/dev/null 2>&1 || die 70 "Internal error: iikoWeb request body is not valid JSON."
  fi

  local response_file http_status
  response_file=$(mktemp "${TMPDIR:-/tmp}/iikoweb-response.XXXXXX") || die 74 "Could not create a temporary response file."
  chmod 600 "$response_file"
  local -a args=(
    --silent --show-error --request "$method"
    --connect-timeout "$IIKO_TIMEOUT" --max-time "$IIKO_TIMEOUT"
    --header 'Accept: application/json, text/plain, */*'
    --header 'Content-Type: application/json'
    --header 'X-Requested-With: XMLHttpRequest'
    --output "$response_file" --write-out '%{http_code}'
  )
  [[ -n "$body_json" ]] && args+=(--data-binary "$body_json")
  [[ -n "$WEB_TOKEN" ]] && args+=(--header "Authorization: Bearer $WEB_TOKEN")
  [[ -n "$WEB_COOKIE_HEADER" ]] && args+=(--cookie "$WEB_COOKIE_HEADER")
  [[ -n "$WEB_COOKIE_JAR" ]] && args+=(--cookie "$WEB_COOKIE_JAR" --cookie-jar "$WEB_COOKIE_JAR")

  set +e
  http_status=$("$IIKO_CURL_BIN" "${args[@]}" "${IIKO_SERVER_URL%/}$path")
  local request_status=$?
  set -e
  if ((request_status != 0)); then
    rm -f "$response_file"
    die 69 "iikoWeb request failed before receiving a response."
  fi
  if [[ ! "$http_status" =~ ^2[0-9][0-9]$ ]]; then
    local description
    description=$(jq -r '.errorDescription // .error // .message // empty' "$response_file" 2>/dev/null || true)
    [[ -n "$description" ]] || description=$(tr '\n' ' ' <"$response_file" | cut -c1-240)
    rm -f "$response_file"
    die 69 "iikoWeb request failed (HTTP $http_status): ${description:-no details}"
  fi
  if ! jq -e . "$response_file" >/dev/null 2>&1; then
    rm -f "$response_file"
    die 69 "iikoWeb returned invalid JSON (HTTP $http_status)."
  fi
  cat "$response_file"
  rm -f "$response_file"
}

web_auth() {
  WEB_TOKEN=""
  WEB_COOKIE_HEADER=""
  WEB_COOKIE_JAR=""

  if [[ -n "$IIKO_COOKIE" ]]; then
    if [[ "$IIKO_COOKIE" == eyJ*.* ]]; then
      WEB_TOKEN=$IIKO_COOKIE
    else
      WEB_COOKIE_HEADER=$IIKO_COOKIE
    fi
    return 0
  fi

  WEB_COOKIE_JAR=$(mktemp "${TMPDIR:-/tmp}/iikoweb-cookie.XXXXXX") || die 74 "Could not create a temporary cookie file."
  chmod 600 "$WEB_COOKIE_JAR"
  trap cleanup_web_session EXIT
  local payload response
  payload=$(jq -nc --arg login "$IIKO_LOGIN" --arg password "$IIKO_PASSWORD" '{login:$login,password:$password}')
  response=$(web_request POST /api/auth/login "$payload")
  if jq -e '.authorized == false or .error == true' <<<"$response" >/dev/null 2>&1; then
    die 69 "iikoWeb rejected the supplied login credentials."
  fi
  WEB_TOKEN=$(jq -r '.token // empty' <<<"$response")
  if [[ -z "$WEB_TOKEN" && ! -s "$WEB_COOKIE_JAR" ]]; then
    die 69 "iikoWeb login returned neither a token nor a session cookie."
  fi
}

stores_command() {
  local format=table
  while (($#)); do
    case "$1" in
      --json) format=json ;;
      -h|--help)
        printf 'Usage: ./iiko.sh stores [--json]\n'
        return 0
        ;;
      *) die 2 "Unknown stores option: $1" ;;
    esac
    shift
  done
  require_dependencies
  validate_web_config
  web_auth
  local response
  response=$(web_request GET /api/stores/list '')
  jq -e '.stores | type == "array"' <<<"$response" >/dev/null || die 69 "iikoWeb response does not contain a stores array."
  if [[ "$format" == json ]]; then
    jq . <<<"$response"
  else
    printf 'ID\tNAME\n'
    jq -r '.stores[] | [.id, .name] | @tsv' <<<"$response"
  fi
}

validate_date() {
  local value=$1
  [[ "$value" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die 2 "Date must use YYYY-MM-DD: $value"
  if date -d "$value" +%F >/dev/null 2>&1; then
    [[ $(date -d "$value" +%F) == "$value" ]] || die 2 "Invalid calendar date: $value"
  elif date -j -f '%Y-%m-%d' "$value" '+%Y-%m-%d' >/dev/null 2>&1; then
    [[ $(date -j -f '%Y-%m-%d' "$value" '+%Y-%m-%d') == "$value" ]] || die 2 "Invalid calendar date: $value"
  fi
}

default_yesterday() {
  if date -d yesterday +%F >/dev/null 2>&1; then
    date -d yesterday +%F
  else
    date -v-1d +%F
  fi
}

sales_filters_json() {
  local date_from=$1 date_to=$2
  jq -nc --arg from "$date_from" --arg to "$date_to" '[
    {filterType:"date_range",dateFrom:$from,dateTo:$to,includeLeft:true,includeRight:true,field:"OpenDate.Typed"},
    {field:"DeletedWithWriteoff",filterType:"value_list",dateFrom:null,dateTo:null,valueMin:null,valueMax:null,valueList:["NOT_DELETED"],includeLeft:true,includeRight:false,inclusiveList:true},
    {field:"OrderDeleted",filterType:"value_list",dateFrom:null,dateTo:null,valueMin:null,valueMax:null,valueList:["NOT_DELETED"],includeLeft:true,includeRight:false,inclusiveList:true}
  ]'
}

web_olap_report() {
  local body=$1 init fetch_id grouped attempt error_file request_status delay_seconds
  init=$(web_request POST /api/olap/init "$body")
  fetch_id=$(jq -er '.fetchId // .data | select(type == "string" and length > 0)' <<<"$init") ||
    die 69 "iikoWeb OLAP initialization did not return a fetch ID."
  [[ "$fetch_id" =~ ^[A-Za-z0-9._-]+$ ]] || die 69 "iikoWeb returned an unsafe fetch ID."

  error_file=$(mktemp "${TMPDIR:-/tmp}/iikoweb-olap-error.XXXXXX") || die 74 "Could not create a temporary error file."
  for ((attempt=1; attempt<=IIKO_GROUPED_RETRY_COUNT; attempt++)); do
    set +e
    grouped=$(web_request POST "/api/olap/fetch/$fetch_id/grouped-table" "$body" 2>"$error_file")
    request_status=$?
    set -e
    if ((request_status == 0)); then
      rm -f "$error_file"
      printf '%s' "$grouped"
      return 0
    fi
    if ! grep -Eqi 'Data not found|Request data not found' "$error_file" || ((attempt == IIKO_GROUPED_RETRY_COUNT)); then
      cat "$error_file" >&2
      rm -f "$error_file"
      return "$request_status"
    fi
    delay_seconds=$(jq -nr --argjson ms "$IIKO_GROUPED_RETRY_DELAY_MS" '$ms / 1000')
    sleep "$delay_seconds"
  done
}

revenue_command() {
  local target_date="" output=""
  while (($#)); do
    case "$1" in
      --date) (($# >= 2)) || die 2 "--date requires YYYY-MM-DD."; target_date=$2; shift 2 ;;
      --output) (($# >= 2)) || die 2 "--output requires a file path."; output=$2; shift 2 ;;
      -h|--help) printf 'Usage: ./iiko.sh revenue [--date YYYY-MM-DD] [--output FILE]\n'; return 0 ;;
      *) die 2 "Unknown revenue option: $1" ;;
    esac
  done
  target_date=${target_date:-$(default_yesterday)}
  validate_date "$target_date"
  require_dependencies
  validate_web_config
  web_auth
  local filters payload raw normalized
  filters=$(sales_filters_json "$target_date" "$target_date")
  payload=$(jq -nc --argjson storeId "$IIKO_STORE_ID" --argjson filters "$filters" '{storeIds:[$storeId],olapType:"SALES",groupFields:["PayTypes.Combo"],dataFields:["UniqOrderId.OrdersCount","DishDiscountSumInt.average","DishDiscountSumInt"],calculatedFields:[],filters:$filters,includeVoidTransactions:false,includeNonBusinessPaymentTypes:false}')
  raw=$(web_olap_report "$payload")
  normalized=$(jq --arg date "$target_date" --argjson storeId "$IIKO_STORE_ID" '{date:$date,storeId:$storeId,rows:[.result.rows[]? | {paymentType:(.field0.value // ""),ordersCount:((.field1.value // 0) | tonumber? // 0),averageCheck:((.field2.value // 0) | tonumber? // 0),revenue:((.field3.value // 0) | tonumber? // 0)}]} | .totalRevenue = ([.rows[].revenue] | add // 0)' <<<"$raw")
  write_json_output "$normalized" "$output"
}

dish_sales_command() {
  local date_from="" date_to="" output=""
  while (($#)); do
    case "$1" in
      --from) (($# >= 2)) || die 2 "--from requires YYYY-MM-DD."; date_from=$2; shift 2 ;;
      --to) (($# >= 2)) || die 2 "--to requires YYYY-MM-DD."; date_to=$2; shift 2 ;;
      --output) (($# >= 2)) || die 2 "--output requires a file path."; output=$2; shift 2 ;;
      -h|--help) printf 'Usage: ./iiko.sh dish-sales [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--output FILE]\n'; return 0 ;;
      *) die 2 "Unknown dish-sales option: $1" ;;
    esac
  done
  date_to=${date_to:-$(default_yesterday)}
  date_from=${date_from:-$date_to}
  validate_date "$date_from"
  validate_date "$date_to"
  [[ "$date_from" < "$date_to" || "$date_from" == "$date_to" ]] || die 2 "--from must not be later than --to."
  require_dependencies
  validate_web_config
  web_auth
  local filters payload raw normalized
  filters=$(sales_filters_json "$date_from" "$date_to")
  payload=$(jq -nc --argjson storeId "$IIKO_STORE_ID" --argjson filters "$filters" '{storeIds:[$storeId],olapType:"SALES",groupFields:["DishId","DishName","DishCategory.Accounting"],dataFields:["DishAmountInt","DishDiscountSumInt"],calculatedFields:[],filters:$filters,includeVoidTransactions:false,includeNonBusinessPaymentTypes:false}')
  raw=$(web_olap_report "$payload")
  normalized=$(jq --arg from "$date_from" --arg to "$date_to" --argjson storeId "$IIKO_STORE_ID" '{dateFrom:$from,dateTo:$to,storeId:$storeId,rows:[.result.rows[]? | {productId:(.field0.value // ""),name:(.field1.value // ""),accountingCategory:(.field2.value // ""),quantity:((.field3.value // 0) | tonumber? // 0),revenue:((.field4.value // 0) | tonumber? // 0)}] | sort_by(.name,.productId)}' <<<"$raw")
  write_json_output "$normalized" "$output"
}

doctor_command() {
  require_dependencies
  local mode
  mode=$(detect_mode)
  if [[ "$mode" == web ]]; then
    validate_web_config
    printf 'Local configuration: OK (iikoWeb)\n'
    if [[ ${IIKO_SKIP_LIVE_DOCTOR:-0} == 1 ]]; then
      return 0
    fi
    web_auth
    local auth_response stores_response store_count
    auth_response=$(web_request GET /api/auth '')
    jq -e '.authorized != false' <<<"$auth_response" >/dev/null || die 69 "iikoWeb did not confirm authorization."
    stores_response=$(web_request GET /api/stores/list '')
    store_count=$(jq -er '.stores | length' <<<"$stores_response") || die 69 "iikoWeb response does not contain stores."
    jq -e --argjson id "$IIKO_STORE_ID" '.stores | any((.id | tonumber?) == $id)' <<<"$stores_response" >/dev/null ||
      die 69 "IIKO_STORE_ID is not available to this iikoWeb login."
    printf 'iikoWeb access: OK (%s stores; selected %s)\n' "$store_count" "$IIKO_STORE_ID"
    return 0
  fi

  validate_cloud_config
  printf 'Local configuration: OK\n'
  if [[ ${IIKO_SKIP_LIVE_DOCTOR:-0} == 1 ]]; then
    return 0
  fi
  local response count
  response=$(get_organizations)
  count=$(jq -er '.organizations | length' <<<"$response") ||
    die 69 "iikoCloud response does not contain organizations."
  ((count > 0)) || die 69 "No organizations are available to this API login."
  printf 'API access: OK (%s organizations)\n' "$count"
}

main() {
  load_env
  apply_defaults

  local command=${1:---help}
  case "$command" in
    -h|--help|help)
      usage
      ;;
    doctor)
      shift
      (($# == 0)) || die 2 "doctor does not accept arguments."
      doctor_command
      ;;
    organizations)
      shift
      organizations_command "$@"
      ;;
    nomenclature)
      shift
      nomenclature_command "$@"
      ;;
    yandex-menu)
      shift
      yandex_menu_command "$@"
      ;;
    stores)
      shift
      stores_command "$@"
      ;;
    revenue)
      shift
      revenue_command "$@"
      ;;
    dish-sales)
      shift
      dish_sales_command "$@"
      ;;
    *)
      usage >&2
      die 2 "Unknown command: $command"
      ;;
  esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi
