$projectPath = "C:\Users\andrea.fallavollita\OneDrive - Marketing Multimedia\File di chat di Microsoft Teams\Documenti\richieste linkedln"
$py = "C:\Users\andrea.fallavollita\AppData\Local\Programs\Python\Python312\python.exe"

Set-Location $projectPath

# Compile all Python files
Write-Host "Compiling Python files..."
& $py -m py_compile discovery_agent.py
& $py -m py_compile build_prospect_list.py
& $py -m py_compile check_prospect_counts.py
& $py -m py_compile export_prospects_csv.py

# Initial check
Write-Host "Initial check..."
& $py check_prospect_counts.py

# Build prospect list
Write-Host "Running build_prospect_list.py..."
$dryRun = $false
if ($args -contains "-DryRun") {
    $dryRun = $true
}

if ($dryRun) {
    & $py build_prospect_list.py --target 50 --batch-target-step 10 --min-score 20 --max-queries 20 --max-pages 1 --start-seed 600 --sleep-between-batches 60 --dry-run
} else {
    & $py build_prospect_list.py --target 50 --batch-target-step 10 --min-score 20 --max-queries 20 --max-pages 1 --start-seed 600 --sleep-between-batches 60
}

# Export CSV
Write-Host "Exporting CSV..."
& $py export_prospects_csv.py --status discovered --limit 500 --min-score 20

# Final check
Write-Host "Final check..."
& $py check_prospect_counts.py