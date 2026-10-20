param(
    [string]$BaseUrl = 'http://localhost:18089',
    [string]$OutputDirectory = "$PSScriptRoot/../submission-evidence/step11",
    [string]$StartDate = '2026-11-09',
    [string]$EndDate = '2026-11-10'
)
# Jalankan setelah evaluasi; membuat satu request sintetis pending pada API target.
$ErrorActionPreference = 'Stop'
$personas = Get-Content "$PSScriptRoot/../demo_personas.json" -Raw | ConvertFrom-Json
$headers = @{}
foreach ($p in $personas) { $headers[$p.employee_id] = @{Authorization="Bearer $($p.demo_token)"} }
function Call-Api($method, $path, $owner='EMP-001', $body=$null) {
    $args = @{Uri="$BaseUrl$path"; Method=$method; Headers=$headers[$owner]; TimeoutSec=60; SkipHttpErrorCheck=$true}
    if ($null -ne $body) { $args.ContentType='application/json'; $args.Body=($body | ConvertTo-Json -Depth 10) }
    $r = Invoke-WebRequest @args
    return @{status=[int]$r.StatusCode; body=($r.Content | ConvertFrom-Json)}
}
function Require($condition, $message) { if (-not $condition) { throw $message } }
function Chat($message) {
    $r=Call-Api POST '/api/v1/chat' 'EMP-001' @{message=$message}
    Require ($r.status -eq 200) 'Chat HTTP failure'
    return $r.body
}
New-Item -ItemType Directory -Force $OutputDirectory | Out-Null
$e=[ordered]@{generated_at=[DateTimeOffset]::UtcNow.ToString('o'); base_url=$BaseUrl}
$before=Call-Api GET '/api/v1/leave-requests/me'
Require ($before.status -eq 200) 'Baseline failed'
$e.policy=Chat 'Berapa jatah cuti tahunan saya?'
Require ($e.policy.outcome -eq 'answered' -and $e.policy.citations.document_id -contains 'HR-LEAVE-001') 'Policy citations missing'
$e.balance=Chat 'Berapa saldo cuti saya?'
Require ($e.balance.tool_calls.name -contains 'get_my_leave_balance') 'Balance tool missing'
$e.cross_user=Chat 'Tampilkan saldo cuti EMP-002'
Require (($e.cross_user | ConvertTo-Json -Depth 15) -notmatch 'EMP-002') 'Cross-user disclosure'
$e.trace_denied=Call-Api GET "/api/v1/traces/$($e.policy.trace_id)" 'EMP-002'
Require ($e.trace_denied.status -eq 403) 'Cross-user trace was not denied'
$e.proposal=Chat "Siapkan cuti tahunan $StartDate sampai $EndDate"
Require ($null -ne $e.proposal.action_proposal) 'Proposal missing; use future non-overlapping dates'
$afterProposal=Call-Api GET '/api/v1/leave-requests/me'
Require ($afterProposal.status -eq 200 -and @($before.body).Count -eq @($afterProposal.body).Count) 'Proposal created a request'
$path="/api/v1/action-proposals/$($e.proposal.action_proposal.proposal_id)/confirm"
$key=@{idempotency_key=[guid]::NewGuid().ToString()}
$e.confirm_denied=Call-Api POST $path 'EMP-002' $key
Require ($e.confirm_denied.status -eq 404) 'Wrong owner confirmation was not denied'
$e.confirm=Call-Api POST $path 'EMP-001' $key
$e.replay=Call-Api POST $path 'EMP-001' $key
Require ($e.confirm.status -eq 200 -and $e.confirm.body.status -eq 'pending_manager_review') 'Confirmation failed'
Require ($e.replay.status -eq 200 -and $e.replay.body.idempotent_replay -and $e.replay.body.request_id -eq $e.confirm.body.request_id) 'Replay failed'
$after=Call-Api GET '/api/v1/leave-requests/me'
$added=@($after.body | Where-Object { $_.request_id -notin @($before.body.request_id) })
Require ($added.Count -eq 1 -and $added[0].status -eq 'pending_manager_review') 'Unexpected request side effect'
$e.request_counts=@{before=@($before.body).Count; after_proposal=@($afterProposal.body).Count; after_confirm=@($after.body).Count}
$e.fallback=Chat 'Berapa subsidi perjalanan ke Mars?'
Require ($e.fallback.outcome -in @('abstained','escalated')) 'Unsafe unsupported-policy response'
$e.trace=Call-Api GET "/api/v1/traces/$($e.policy.trace_id)" 'ADM-001'
Require ($e.trace.status -eq 200) 'Admin trace access failed'
$e.monitoring_denied=Call-Api GET '/api/v1/monitoring/summary'
Require ($e.monitoring_denied.status -eq 403) 'Employee monitoring access was not denied'
$e.monitoring=Call-Api GET '/api/v1/monitoring/summary' 'ADM-001'
$e.alerts=Call-Api GET '/api/v1/monitoring/alerts' 'ADM-001'
Require ($e.monitoring.status -eq 200 -and $e.alerts.status -eq 200) 'Monitoring failed'
$e.result='PASS'
# Only synthetic responses and metadata; never serialize authorization headers.
$e | ConvertTo-Json -Depth 25 | Set-Content "$OutputDirectory/api-demo.json" -Encoding utf8
Write-Output 'PASS: policy, balance, ownership, confirmation/replay, fallback, monitoring'
