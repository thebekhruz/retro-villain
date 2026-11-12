#!/usr/bin/env bash
set -u

FIXTURE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/fixtures" && pwd -P)
output_file=""
url=""
body=""
authorization=""
method="GET"
cookie_jar=""

while (($#)); do
  case "$1" in
    --output)
      output_file=$2
      shift 2
      ;;
    --data|--data-raw)
      body=$2
      shift 2
      ;;
    --data-binary)
      if [[ $2 == @* ]]; then
        body=$(<"${2#@}")
      else
        body=$2
      fi
      shift 2
      ;;
    --header)
      if [[ $2 == Authorization:* ]]; then
        authorization=$2
      fi
      shift 2
      ;;
    --cookie-jar)
      cookie_jar=$2
      shift 2
      ;;
    --cookie)
      shift 2
      ;;
    http://*|https://*)
      url=$1
      shift
      ;;
    --request)
      method=$2
      shift 2
      ;;
    --connect-timeout|--max-time|--write-out)
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

if [[ -n ${IIKO_FAKE_CURL_LOG:-} ]]; then
  {
    printf 'URL=%s\n' "$url"
    printf 'BODY=%s\n' "$body"
    printf 'AUTH=%s\n' "$authorization"
    printf 'METHOD=%s\n' "$method"
  } >>"$IIKO_FAKE_CURL_LOG"
fi

case "$url" in
  */access_token)
    fixture="$FIXTURE_DIR/access_token.json"
    status=200
    ;;
  */organizations)
    fixture=${IIKO_FAKE_ORGANIZATIONS_FIXTURE:-"$FIXTURE_DIR/organizations.json"}
    status=200
    ;;
  */nomenclature)
    fixture="$FIXTURE_DIR/nomenclature.json"
    status=200
    ;;
  */api/auth/login)
    fixture="$FIXTURE_DIR/web_login.json"
    status=200
    if [[ -n "$cookie_jar" ]]; then
      printf '#HttpOnly_fixture\tFALSE\t/\tFALSE\t0\ttoken\tweb-fixture-token\n' >"$cookie_jar"
    fi
    ;;
  */api/auth)
    fixture="$FIXTURE_DIR/web_auth.json"
    status=200
    ;;
  */api/stores/list)
    fixture="$FIXTURE_DIR/web_stores.json"
    status=200
    ;;
  */api/olap/init)
    if [[ "$body" == *'Dish.Code'* || "$body" == *'"DishCategory"'* ]]; then
      fixture=""
      status=400
    else
      fixture="$FIXTURE_DIR/web_olap_init.json"
      status=200
    fi
    ;;
  */api/olap/fetch/*/grouped-table)
    if [[ "$body" == *'PayTypes.Combo'* ]]; then
      fixture="$FIXTURE_DIR/web_revenue.json"
    else
      fixture="$FIXTURE_DIR/web_dish_sales.json"
    fi
    status=200
    ;;
  *)
    fixture=""
    status=404
    ;;
esac

if [[ -n "$output_file" ]]; then
  if [[ -n "$fixture" ]]; then
    cp "$fixture" "$output_file"
  elif [[ "$status" == 400 ]]; then
    printf '%s\n' '{"errorDescription":"invalid OLAP field"}' >"$output_file"
  else
    printf '%s\n' '{"errorDescription":"unexpected endpoint"}' >"$output_file"
  fi
else
  if [[ -n "$fixture" ]]; then
    cat "$fixture"
  elif [[ "$status" == 400 ]]; then
    printf '%s\n' '{"errorDescription":"invalid OLAP field"}'
  else
    printf '%s\n' '{"errorDescription":"unexpected endpoint"}'
  fi
fi

printf '%s' "$status"
