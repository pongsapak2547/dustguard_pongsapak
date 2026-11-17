#!/usr/bin/env bash
# เรียก dustguard-checker ด้วยเกณฑ์ต่ำ (ค่าเริ่มต้น 5) เพื่อบังคับให้ส่งอีเมลเตือน ใช้ตอน demo
#   bash scripts/test_alert.sh        # เกณฑ์ 5 µg/m³
#   bash scripts/test_alert.sh 20     # เกณฑ์ 20 µg/m³
set -euo pipefail
export AWS_DEFAULT_REGION=us-east-1 AWS_PAGER=""
THRESHOLD="${1:-5}"
OUT=$(mktemp)
aws lambda invoke --function-name dustguard-checker \
  --cli-binary-format raw-in-base64-out \
  --payload "{\"test_threshold\": ${THRESHOLD}}" "$OUT" >/dev/null
python3 -c 'import sys,json;print(json.dumps(json.load(open(sys.argv[1])),ensure_ascii=False,indent=2))' "$OUT"
rm -f "$OUT"
