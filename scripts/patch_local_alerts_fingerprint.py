#!/usr/bin/env python3
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'scripts'/'build_local_alerts.py'
s=p.read_text(encoding='utf-8')
s=s.replace('import csv\n','import csv\nimport hashlib\n',1)
s=s.replace("fp=str(hash(normalized))", "fp=hashlib.sha256(normalized.encode('utf-8')).hexdigest()")
p.write_text(s,encoding='utf-8')
print('patched deterministic alert fingerprint')
