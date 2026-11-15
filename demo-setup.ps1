$ANS = [ordered]@{'0'=1;'1'=1;'2'=0;'3'=2;'4'=1} | ConvertTo-Json -Compress
$G = '0=1,1=1,2=0,3=2,4=1'
$TRACE = python -c 'import uuid;print(uuid.uuid4().hex)'
$BASE = 'http://localhost:8000/api/v1'
$USER_ID = (Invoke-RestMethod -Method Post -Uri ($BASE + '/auth/register') -ContentType 'application/json' -Body (@{username=('video_' + (Get-Random));password='secret123'} | ConvertTo-Json)).user_id
function new_session { (Invoke-RestMethod -Method Post -Uri ($BASE + '/quizzes/python_101/sessions') -ContentType 'application/json' -Headers @{'X-Trace-Id'=$TRACE} -Body (@{user_id=$USER_ID} | ConvertTo-Json)).session_id }
function http_submit($sid, $answers) { $body = @{user_id=$USER_ID; answers=($answers | ConvertFrom-Json)} | ConvertTo-Json -Compress; $body | curl.exe -s -i -X PUT ($BASE + '/sessions/' + $sid + '/attempt') -H 'content-type: application/json' -H ('X-Trace-Id: ' + $TRACE) --data-binary '@-'; '' }
function grpc_submit($sid, $answers, $deadline = 3, [switch]$anomaly) { $a = @('scripts/grpc_client.py','--session-id',$sid,'--user-id',$USER_ID,'--answers',$answers,'--deadline',$deadline,'--trace-id',$TRACE); if ($anomaly) { $a += '--simulate-anomaly' }; python @a }
function show_logs($c) { docker logs $c 2>&1 | Select-String $TRACE | ForEach-Object { $j = $_.Line | ConvertFrom-Json; ('{0} | {1} | {2}' -f $j.'service.name', $j.'event.action', $j.message) } }
Write-Output $TRACE $USER_ID
