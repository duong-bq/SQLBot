#!/usr/bin/env bash
# Script curl mẫu để test thủ công POST /hooks/ai-sync sau khi đổi envelope
# (chỉ còn actionType + payload, không còn version/timestamp, không còn kiểm STALE).
#
# Cách dùng:
#   HOST=http://localhost:8000 SQLBOT_USER=admin SQLBOT_PASS=Admin@123 ./test_ai_sync_hook.sh
#
# Tài khoản đăng nhập PHẢI có quyền ws_admin (hoặc admin toàn cục) — xem §1
# AI_SYNC_HOOK_API_SPEC.md. Yêu cầu `jq` và `uuidgen` có sẵn trong PATH.

set -euo pipefail

HOST="${HOST:-http://localhost:8000}"
SQLBOT_USER="${SQLBOT_USER:?Thiếu biến môi trường SQLBOT_USER}"
SQLBOT_PASS="${SQLBOT_PASS:?Thiếu biến môi trường SQLBOT_PASS}"

echo "== 1. Đăng nhập lấy access_token =="
TOKEN=$(curl -s -X POST "$HOST/api/v1/login/access-token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=${SQLBOT_USER}&password=${SQLBOT_PASS}" | jq -r .access_token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
  echo "Đăng nhập thất bại — kiểm tra lại SQLBOT_USER/SQLBOT_PASS." >&2
  exit 1
fi
echo "OK, token: ${TOKEN:0:20}..."

echo
echo "== 2. Gọi AUTHORIZATION_SYNC (actionType = 1) =="
curl -s -X POST "$HOST/api/v1/hooks/ai-sync" \
  -H "Content-Type: application/json" \
  -H "X-SQLBOT-TOKEN: Bearer $TOKEN" \
  -H "X-Request-ID: $(uuidgen)" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{
    "actionType": 1,
    "payload": {
      "users": [
        {
          "userId": "usr-test-001",
          "fullName": "Người dùng Test",
          "isAdmin": false,
          "formQueries": [
            {
              "formUuid": "form-test-001",
              "tableInfo": {
                "databaseTableName": "kdl_nhan_khau_row_values",
                "tableDisplayName": "Dữ liệu Nhân khẩu",
                "queries": [
                  {
                    "datasourceId": "7",
                    "datasourceType": "postgresql",
                    "query": "SELECT ho_ten, nam_sinh, province_id FROM kdl_nhan_khau_row_values WHERE province_id = '\''01'\''"
                  }
                ]
              }
            }
          ]
        }
      ]
    }
  }' | jq .

echo
echo "== 3. Gọi DATASOURCE_SYNC (actionType = 4) — sửa datasourceIds cho khớp môi trường =="
curl -s -X POST "$HOST/api/v1/hooks/ai-sync" \
  --max-time 600 \
  -H "Content-Type: application/json" \
  -H "X-SQLBOT-TOKEN: Bearer $TOKEN" \
  -H "X-Request-ID: $(uuidgen)" \
  -H "X-Idempotency-Key: $(uuidgen)" \
  -d '{
    "actionType": 4,
    "payload": {"datasourceIds": ["7"]}
  }' | jq .
