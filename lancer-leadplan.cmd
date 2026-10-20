@echo off
schtasks /run /tn LeadPlan
echo LeadPlan lance en arriere-plan (environ 5 min). Email a la fin, journal dans leadplan.log
pause
