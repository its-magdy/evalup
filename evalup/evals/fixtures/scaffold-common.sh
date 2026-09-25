# Sourced by each case's fixture.sh. $HERE is the case directory; the cwd is
# the run's empty workspace. Builds ./app (a git repo holding the demo server)
# and starts the server on $PORT as the person running the eval, outside the
# agent's sandbox, so it survives into the run.
FIX="$HERE/../fixtures"
mkdir -p app
cp "$FIX/helpdesk_server.py" app/server.py
cp "$FIX/app-README.md" app/README.md
git -C app init -q
git -C app -c user.email=eval@example.invalid -c user.name=eval add -A
git -C app -c user.email=eval@example.invalid -c user.name=eval commit -q -m "demo app"
pkill -f "server.py $PORT" 2>/dev/null || true
nohup python3 "$PWD/app/server.py" "$PORT" >/dev/null 2>&1 &
sleep 1
