COMMENT HUB - all your comments on one phone page, with suggested replies and one tap to send
==============================================================================================
It shows the comments on FragSpree + Brain Massage, Instagram + YouTube, a suggested reply under each one, and a green button
"Approve & send N replies". Spam and negative comments are never ticked. Nothing is sent until you press the button.
Replies go out slowly (a random 12-35 second pause between them) and there is a daily limit, so it looks human.

SET UP (once)
-------------
A. On your PC (only needed for YouTube comments):
   1. Run videos\ig_post\youtube_comment_signin_FragSpree.bat and ...BrainMassage.bat (browser sign-in, click Allow).
   2. Double-click  copy_tokens.bat  in this folder: it copies the two YouTube sign-in files + client_secret.json next to hub.py.
   3. Instagram: make sure the tokens in C:\fragspree\.env and C:\brain_poster\.env (on the VPS) include the comments permission
      (instagram_business_manage_comments) - the hub reads the Instagram id + token from those two files.

B. Copy this whole folder to the VPS as   C:\comment_hub
   (it contains your YouTube sign-in files and the page password - delete the folder from your PC afterwards).

C. On the VPS open .env in Notepad:
   - HUB_PASSWORD is the password for the page (already set to a random one - change it if you like, at least 8 characters).
   - DRY_RUN=1 means TEST MODE: the page works but nothing is posted. Change to DRY_RUN=0 when you are happy.
   - GEMINI_API_KEY=...  lets AI write the replies (copy_tokens.bat fills it in from your PC if you have one; free key at https://aistudio.google.com/apikey).
     The AI is given each reel's real question, correct answer and explanation (video_context.json, 530 reels), so a reply to "is it 14?" under the
     two-eggs reel checks it against the real answer. Without a key the page uses simple rules: it compares the number in the comment with the real
     answer ("Correct!" / "Good try, watch the explanation again") and thanks people for praise.

D. On the VPS open PowerShell as administrator and paste this whole block:

cd C:\comment_hub
python -m pip install requests
$py = (Get-Command python).Source
$s = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
$t = New-ScheduledTaskTrigger -AtStartup
$a = New-ScheduledTaskAction -Execute $py -Argument "C:\comment_hub\hub.py" -WorkingDirectory "C:\comment_hub"
Register-ScheduledTask -TaskName CommentHub -Action $a -Trigger $t -Settings $s -RunLevel Highest -User SYSTEM -Force
$b = New-ScheduledTaskAction -Execute "C:\comment_hub\cloudflared.exe" -Argument "tunnel --url http://127.0.0.1:8780 --logfile C:\comment_hub\tunnel.log" -WorkingDirectory "C:\comment_hub"
Register-ScheduledTask -TaskName CommentHubTunnel -Action $b -Trigger $t -Settings $s -RunLevel Highest -User SYSTEM -Force
Start-ScheduledTask -TaskName CommentHub
Start-ScheduledTask -TaskName CommentHubTunnel
Start-Sleep 15
Select-String -Path C:\comment_hub\tunnel.log -Pattern "https://.*trycloudflare.com" | Select -Last 1

E. The last line prints your page address (https://something.trycloudflare.com). Open it on your phone, type the password,
   and add it to your home screen. The address stays the same until the VPS restarts; if it ever changes, run show_url.bat on the VPS.
   (The tunnel is free, needs no account, and gives you HTTPS, so the password is never sent in the clear.)

USING IT
--------
- Open the page and tap Refresh (it also refreshes the first time you open it).
- Chips at the top filter by account/platform or by type (questions, negative, spam).
- Edit any suggested reply by tapping it. Untick "send this reply" or tap Skip for the ones you do not want to answer.
- Tap the green button. Replies are sent one by one; the bar shows progress. Already-sent ones are greyed out.
- Daily limits (in .env): 120 Instagram replies, 60 YouTube replies (each YouTube reply costs 50 of your 10,000 daily YouTube quota units).

SAFETY
------
- Keep the password private. Five wrong tries lock that device out for 10 minutes.
- Replies can only be removed by deleting them in the Instagram / YouTube app, so skim the suggestions before you approve.
- Links are always stripped from replies.
