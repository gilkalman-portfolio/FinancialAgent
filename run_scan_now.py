"""
Quick Scan - Run immediate scan without scheduling

Use this to test the scanner or run a quick manual scan.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from src.automated_scanner import AutomatedScanner
from datetime import datetime

print("\n" + "="*70)
print("  QUICK SCAN - Manual Run")
print("="*70 + "\n")

# Initialize scanner
scanner = AutomatedScanner()

# Run immediate scan
print(f"Starting scan at {datetime.now().strftime('%H:%M:%S')}...\n")

try:
    scanner.run_scan("Manual")
    print("\n" + "="*70)
    print("  SCAN COMPLETE!")
    print("="*70)
    
    # Show summary
    reports_dir = Path('reports')
    if reports_dir.exists():
        latest_report = max(reports_dir.glob('scan_*.json'))
        print(f"\nReport saved: {latest_report.name}")
        
        import json
        with open(latest_report, 'r') as f:
            results = json.load(f)
        
        critical = [r for r in results if r['score'] >= 85]
        high = [r for r in results if 70 <= r['score'] < 85]
        moderate = [r for r in results if 60 <= r['score'] < 70]
        
        print(f"\nResults:")
        print(f"  Critical (85+):  {len(critical)}")
        print(f"  High (70-84):    {len(high)}")
        print(f"  Moderate (60+):  {len(moderate)}")
        print(f"  Total scanned:   {len(results)}")
        
        if critical:
            print("\n[!] Critical Opportunities:")
            for r in critical[:5]:
                print(f"  - {r['ticker']}: {r['score']:.1f}/100 (${r['price']:.2f})")
        
        if high:
            print("\n[!] High Conviction:")
            for r in high[:5]:
                print(f"  - {r['ticker']}: {r['score']:.1f}/100")

except KeyboardInterrupt:
    print("\n\n[!] Scan interrupted by user")
except Exception as e:
    print(f"\n[X] Error: {e}")
    import traceback
    traceback.print_exc()

print()
