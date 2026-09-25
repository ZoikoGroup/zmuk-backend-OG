import json

with open('data_backup.json', 'rb') as f:
    content = f.read()

content = content.decode('utf-8', errors='ignore')

with open('data_backup_clean.json', 'w', encoding='utf-8') as f:
    f.write(content)

print('Done - data_backup_clean.json created')