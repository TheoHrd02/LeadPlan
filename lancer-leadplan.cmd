@echo off
schtasks /run /tn LeadPlan
echo LeadPlan lance en arriere-plan (15 a 20 min par region). Un email par region, journal dans leadplan.log
pause
