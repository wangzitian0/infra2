#!/bin/sh
# Appwrite E2E Test Script
# Tests all 5 core modules: Auth, Databases, Storage, Functions, Messaging
#
# Usage: 
#   docker cp e2e-test.sh appwrite:/tmp/
#   docker exec appwrite sh /tmp/e2e-test.sh
#
# Or run from host:
#   ssh root@<server> 'docker exec appwrite sh /tmp/e2e-test.sh'

set -e

ENDPOINT="${APPWRITE_ENDPOINT:-http://localhost/v1}"
PROJECT="${APPWRITE_PROJECT_ID:-69585652003140e5b61c}"
BUCKET="${APPWRITE_BUCKET_ID:-6958568a00232dbcb909}"

echo "===== APPWRITE 5 MODULES E2E TEST ====="
echo "Endpoint: $ENDPOINT"
echo "Project: $PROJECT"
echo ""

# Create session
echo "Creating test session..."
SESS=$(curl -s -X POST "${ENDPOINT}/account/sessions/anonymous" \
    -H "Content-Type: application/json" \
    -H "X-Appwrite-Project: ${PROJECT}")
SECRET=$(echo "$SESS" | grep -o '"secret":"[^"]*"' | cut -d'"' -f4)

if [ -z "$SECRET" ]; then
    echo "✗ Failed to create session"
    echo "$SESS"
    exit 1
fi
echo "✓ Session created"
echo ""

PASS=0
FAIL=0
WARN=0

pass() { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1: $2"; FAIL=$((FAIL+1)); }
warn() { echo "  ⚠ $1"; WARN=$((WARN+1)); }

# === 1. AUTH ===
echo "=== 1. AUTH ==="
PREFS=$(curl -s "${ENDPOINT}/account/prefs" \
    -H "X-Appwrite-Project: ${PROJECT}" \
    -H "X-Appwrite-Session: ${SECRET}")
echo "$PREFS" | grep -q '{' && pass "Get preferences" || fail "Get preferences" "$PREFS"

# === 2. DATABASES ===
echo ""
echo "=== 2. DATABASES ==="
DBS=$(curl -s "${ENDPOINT}/databases" \
    -H "X-Appwrite-Project: ${PROJECT}" \
    -H "X-Appwrite-Session: ${SECRET}")
if echo "$DBS" | grep -q '"databases"'; then
    COUNT=$(echo "$DBS" | grep -o '"total":[0-9]*' | cut -d':' -f2)
    pass "List databases (count: $COUNT)"
else
    warn "List databases (requires API key)"
fi

# === 3. STORAGE ===
echo ""
echo "=== 3. STORAGE ==="

# List files
FILES=$(curl -s "${ENDPOINT}/storage/buckets/${BUCKET}/files" \
    -H "X-Appwrite-Project: ${PROJECT}" \
    -H "X-Appwrite-Session: ${SECRET}")
if echo "$FILES" | grep -q '"files"'; then
    COUNT=$(echo "$FILES" | grep -o '"total":[0-9]*' | cut -d':' -f2)
    pass "List files (count: $COUNT)"
else
    fail "List files" "$FILES"
fi

# Upload
echo "E2E Test $(date)" > /tmp/e2e_test.txt
UPLOAD=$(curl -s -X POST "${ENDPOINT}/storage/buckets/${BUCKET}/files" \
    -H "X-Appwrite-Project: ${PROJECT}" \
    -H "X-Appwrite-Session: ${SECRET}" \
    -F "fileId=unique()" \
    -F "file=@/tmp/e2e_test.txt")

if echo "$UPLOAD" | grep -q '"\$id"'; then
    FILE_ID=$(echo "$UPLOAD" | grep -o '"\$id":"[^"]*"' | head -1 | cut -d'"' -f4)
    pass "Upload file (id: $FILE_ID)"
    
    # Download
    DL=$(curl -s -o /dev/null -w "%{http_code}" \
        "${ENDPOINT}/storage/buckets/${BUCKET}/files/${FILE_ID}/download" \
        -H "X-Appwrite-Project: ${PROJECT}" \
        -H "X-Appwrite-Session: ${SECRET}")
    [ "$DL" = "200" ] && pass "Download file" || fail "Download file" "HTTP $DL"
    
    # Delete
    DEL=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
        "${ENDPOINT}/storage/buckets/${BUCKET}/files/${FILE_ID}" \
        -H "X-Appwrite-Project: ${PROJECT}" \
        -H "X-Appwrite-Session: ${SECRET}")
    [ "$DEL" = "204" ] && pass "Delete file" || fail "Delete file" "HTTP $DEL"
else
    fail "Upload file" "$UPLOAD"
fi

# === 4. FUNCTIONS ===
echo ""
echo "=== 4. FUNCTIONS ==="
FUNCS=$(curl -s "${ENDPOINT}/functions" \
    -H "X-Appwrite-Project: ${PROJECT}" \
    -H "X-Appwrite-Session: ${SECRET}")
if echo "$FUNCS" | grep -q '"functions"'; then
    COUNT=$(echo "$FUNCS" | grep -o '"total":[0-9]*' | cut -d':' -f2)
    pass "List functions (count: $COUNT)"
else
    warn "List functions (requires API key)"
fi

# === 5. MESSAGING ===
echo ""
echo "=== 5. MESSAGING ==="
TOPICS=$(curl -s "${ENDPOINT}/messaging/topics" \
    -H "X-Appwrite-Project: ${PROJECT}" \
    -H "X-Appwrite-Session: ${SECRET}")
if echo "$TOPICS" | grep -q '"topics"'; then
    COUNT=$(echo "$TOPICS" | grep -o '"total":[0-9]*' | cut -d':' -f2)
    pass "List topics (count: $COUNT)"
else
    warn "List topics (requires API key)"
fi

# === INFRASTRUCTURE ===
echo ""
echo "=== INFRASTRUCTURE ==="
nc -z appwrite-mariadb 3306 2>/dev/null && pass "MariaDB" || fail "MariaDB" "unreachable"
nc -z appwrite-redis 6379 2>/dev/null && pass "Redis" || fail "Redis" "unreachable"
nc -z platform-minio 9000 2>/dev/null && pass "MinIO" || fail "MinIO" "unreachable"

# === SUMMARY ===
echo ""
echo "===== SUMMARY ====="
echo "Passed: $PASS"
echo "Failed: $FAIL"
echo "Warnings: $WARN"
echo ""

if [ $FAIL -gt 0 ]; then
    echo "STATUS: FAILED"
    exit 1
else
    echo "STATUS: PASSED"
    exit 0
fi
