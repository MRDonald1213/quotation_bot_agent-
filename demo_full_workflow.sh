#!/bin/bash
set -e

echo "=== Freight Agent Full Workflow Demo ==="
echo ""

# Check if Ollama is running
if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
    echo "❌ Ollama is not running. Please start Ollama first."
    exit 1
fi

echo "✓ Ollama is running"
echo ""

# Step 1: Create shipment
echo "Step 1: Creating shipment..."
SHIPMENT_REF="DEMO-$(date +%Y%m%d-%H%M%S)"
freight shipment create "$SHIPMENT_REF"
echo "✓ Shipment created: $SHIPMENT_REF"
echo ""

# Step 2: Add intake document (using test file if exists)
TEST_FILE="tests/fixtures/sample_quotes/freight_quote_test_workflow_b.txt"
if [ ! -f "$TEST_FILE" ]; then
    echo "Test file not found. Creating a sample quote file..."
    mkdir -p tests/fixtures/sample_quotes
    cat > "$TEST_FILE" << 'EOF'
FREIGHT QUOTATION

From: Shanghai Port, China
To: Hamburg Port, Germany
Shipping method: Sea freight (FCL 20ft container)

Charges:
- Ocean freight: USD 2,850.00
- BAF (Bunker Adjustment Factor): USD 180.00
- Origin handling: CNY 1,200.00
- Destination handling: EUR 245.00
- Customs clearance: EUR 125.00

Transit time: 28-32 days
Validity: 30 days from quote date
Quote reference: SH-HH-2026-001

Terms: FOB Shanghai
Payment: 30 days net

Carrier: Maersk Line
Contact: carrier@example.com
EOF
    echo "✓ Sample quote file created"
fi

echo ""
echo "Step 2: Adding intake document..."
INTAKE_OUTPUT=$(freight intake add "$SHIPMENT_REF" "$TEST_FILE")
echo "$INTAKE_OUTPUT"

# Extract intake ID (first 8 chars)
INTAKE_ID=$(echo "$INTAKE_OUTPUT" | grep -o 'id=[a-f0-9-]*' | head -1 | cut -d'=' -f2 | cut -c1-8)
echo "✓ Intake ID (truncated): $INTAKE_ID"
echo ""

# Step 3: Classify
echo "Step 3: Classifying document (this may take a moment)..."
freight intake classify "$INTAKE_ID"
echo "✓ Classification complete"
echo ""

# Step 4: Process
echo "Step 4: Processing and extracting quote data (this may take a moment)..."
freight intake process "$INTAKE_ID"
echo "✓ Processing complete"
echo ""

# Step 5: Generate clarification questions
echo "Step 5: Generating clarification questions (this may take a moment)..."
freight questions generate "$SHIPMENT_REF"
echo "✓ Questions generated"
echo ""

# Step 6: Show results
echo "Step 6: Summary"
echo "==================="
echo ""
echo "Shipments:"
freight shipment list
echo ""
echo "Intake items:"
freight intake list
echo ""
echo "Quotations:"
freight quote list --shipment-id "$SHIPMENT_REF"
echo ""
echo "Questions:"
freight questions list "$SHIPMENT_REF"
echo ""
echo "Email drafts:"
freight email list
echo ""
echo "Data gaps:"
freight gaps "$SHIPMENT_REF"
echo ""

echo "=== Demo Complete ==="
echo ""
echo "Open TablePlus to see all the data:"
echo "  freight db open"
echo ""
echo "Or get the database path:"
echo "  freight db path"
