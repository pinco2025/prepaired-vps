import csv
import json
import os

from pathlib import Path

# Get the path relative to this script
BASE_DIR = Path(__file__).resolve().parent.parent
INPUT_CSV = BASE_DIR / "source-csv" / "questions.csv"
OUTPUT_JSON = BASE_DIR / "scripts" / "adv_01_ids.json"

def main():
    ids = []
    # Increase field size limit for large CSV fields
    csv.field_size_limit(10**7)
    
    with open(INPUT_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            metadata_str = row.get("metadata", "")
            if metadata_str and "SET-ADV-01" in metadata_str:
                try:
                    metadata = json.loads(metadata_str)
                    if metadata.get("set_id") == "SET-ADV-01":
                        ids.append(row["id"])
                except Exception as e:
                    pass
                    
    print(f"Found {len(ids)} IDs with set_id = 'SET-ADV-01'")
    
    with open(OUTPUT_JSON, "w", encoding="utf-8") as out:
        json.dump(ids, out, indent=2)
        
    print(f"Saved to {OUTPUT_JSON}")

if __name__ == "__main__":
    main()
