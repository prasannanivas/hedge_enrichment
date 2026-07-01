#!/bin/sh
set -e

: "${GOOGLE_API_KEY:?GOOGLE_API_KEY env var is required}"

MODEL="${MODEL:-google:gemini-2.5-flash}"
RPM="${RPM:-14}"
INPUT="${INPUT:-all_managers.csv}"
OUTPUT="${OUTPUT:-/data/enriched_contacts_full.csv}"
RESUME="${RESUME:-}"

echo "=== Hedge Enrichment Agent ==="
echo "Model  : $MODEL"
echo "RPM    : $RPM"
echo "Input  : $INPUT"
echo "Output : $OUTPUT"
echo "Resume : ${RESUME:-no}"
echo "=============================="

if [ -n "$RESUME" ]; then
  exec python agent.py \
    --key    "$GOOGLE_API_KEY" \
    --model  "$MODEL" \
    --rpm    "$RPM" \
    --input  "$INPUT" \
    --output "$OUTPUT" \
    --resume
else
  exec python agent.py \
    --key    "$GOOGLE_API_KEY" \
    --model  "$MODEL" \
    --rpm    "$RPM" \
    --input  "$INPUT" \
    --output "$OUTPUT"
fi
