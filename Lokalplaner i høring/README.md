### Link validation
Go to *"Lokalplaner i høring"* dir, and run:

```python
python validate_links.py
```

This checks all links in *Lokalplaner i høring/municipality_hearing_links.js* and generates a JSON result report in *Lokalplaner i høring/validation_results*, and updates 
 *Lokalplaner i høring/known_issues.json* to reflect the status as per the latest scan.