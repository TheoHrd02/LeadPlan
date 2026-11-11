@echo off
schtasks /run /tn LeadPlan
echo LeadPlan lance en arriere-plan (environ 7 min par region). Un email par region, journal dans leadplan.log
pause
