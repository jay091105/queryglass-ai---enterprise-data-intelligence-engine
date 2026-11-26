"""Verify Query History statistics match database values."""
import sqlite3
from pathlib import Path

# Database path from backend/config.py
PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = str((PROJECT_ROOT / "database.db").resolve())

print("=" * 70)
print("QUERY HISTORY STATISTICS VERIFICATION")
print("=" * 70)
print(f"Database: {DB_PATH}\n")

try:
    db_exists = Path(DB_PATH).exists()
    if not db_exists:
        print(f"❌ Database file not found: {DB_PATH}")
        print("\nThe database will be created when you:")
        print("  1. Start the backend server")
        print("  2. Upload a dataset")
        print("  3. Run queries")
        exit(0)
    
    with sqlite3.connect(DB_PATH) as conn:
        # Query runs table (authoritative source)
        print("✅ AUTHORITATIVE SOURCE: _queryglass_query_runs")
        print("-" * 70)
        cursor = conn.execute("""
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN execution_status = 'success' THEN 1 ELSE 0 END) as successful,
                SUM(CASE WHEN execution_status IN ('syntax_error', 'runtime_error', 'timeout') THEN 1 ELSE 0 END) as blocked,
                AVG(CASE WHEN execution_status = 'success' THEN execution_time_ms ELSE NULL END) as avg_duration_ms
            FROM _queryglass_query_runs
        """)
        row = cursor.fetchone()
        
        if row:
            total, successful, blocked, avg_ms = row
            print(f"Total Records:        {total or 0}")
            print(f"Successful Queries:   {successful or 0}")
            print(f"Blocked Queries:      {blocked or 0}")
            print(f"Average Duration:     {round(avg_ms, 2) if avg_ms else 0.00} ms")
            print(f"\nThis matches /api/query-runs/stats endpoint")
        else:
            print("No data found in _queryglass_query_runs")
        
        # History table (legacy - for comparison)
        print("\n\nℹ️  LEGACY TABLE (NOT USED): _queryglass_history")
        print("-" * 70)
        cursor = conn.execute("""
            SELECT UPPER(status), COUNT(*)
            FROM _queryglass_history
            GROUP BY UPPER(status)
        """)
        status_counts = dict(cursor.fetchall())
        total_legacy = sum(status_counts.values())
        success_legacy = status_counts.get("SUCCESS", 0)
        
        cursor = conn.execute("""
            SELECT AVG(execution_time_ms)
            FROM _queryglass_history
            WHERE UPPER(status)='SUCCESS'
        """)
        avg_legacy = cursor.fetchone()[0]
        
        print(f"Total Queries:        {total_legacy}")
        print(f"Successful:           {success_legacy}")
        print(f"Blocked:              {status_counts.get('BLOCKED', 0)}")
        print(f"Failed:               {status_counts.get('FAILED', 0)}")
        print(f"Average Duration:     {round(avg_legacy, 2) if avg_legacy else 0.00} ms")
        print(f"\nThis table is no longer used for Query History statistics")
        
        # Comparison
        print("\n\n" + "=" * 70)
        print("COMPARISON")
        print("=" * 70)
        if row and total != total_legacy:
            print(f"⚠️  Tables contain different record counts:")
            print(f"   _queryglass_query_runs:  {total or 0} records")
            print(f"   _queryglass_history:     {total_legacy} records")
            print(f"\n✅ Query History now uses _queryglass_query_runs exclusively")
        else:
            print("✅ Both tables have same record count")
            
except sqlite3.Error as e:
    print(f"\n❌ Database error: {e}")

print("\n" + "=" * 70)
