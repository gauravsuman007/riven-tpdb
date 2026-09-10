#!/bin/sh

# Default PUID and PGID to 1000 if not set
PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo "Starting Container with $PUID:$PGID permissions..."

if [ "$PUID" = "0" ]; then
    echo "Running as root user"
    USER_HOME="/root"
else
    # --- User and Group Management ---
    USERNAME=${USERNAME:-riven}
    GROUPNAME=${GROUPNAME:-riven}
    USER_HOME="/home/$USERNAME"
    if ! getent group "$PGID" > /dev/null; then addgroup --gid "$PGID" "$GROUPNAME"; fi
    GROUPNAME=$(getent group "$PGID" | cut -d: -f1)
    if ! getent passwd "$USERNAME" > /dev/null; then adduser -D -h "$USER_HOME" -u "$PUID" -G "$GROUPNAME" "$USERNAME"; fi
    usermod -u "$PUID" -g "$PGID" "$USERNAME"
    adduser "$USERNAME" wheel
fi

# Set home directory permissions and environment
mkdir -p "$USER_HOME"
chown -R "$PUID:$PGID" "$USER_HOME"
export HOME="$USER_HOME"

# The data directory is a bind mount; Docker creates a missing source dir as
# root, so it must be handed to the runtime user or the first write fails.
if [ "$PUID" != "0" ]; then
    mkdir -p /riven/data
    chown -R "$PUID:$PGID" /riven/data
fi

# Second line of defence against a stale FUSE mount.
#
# The library bind mount is `rshared`, so the RivenVFS mount this process
# makes propagates OUT to the host. If the container then dies without
# unmounting -- a crash, a SIGKILL, the docker daemon going down underneath
# it -- the mount survives on the host as a dead endpoint, and every later
# start binds that dead endpoint back in.
#
# This check is NOT the cure, and must not be mistaken for it. Confirmed on
# the server: the daemon usually refuses to create the container at all
# ("invalid mount config for type bind: ... transport endpoint is not
# connected"), so this script never runs. That case is handled outside, by
# the `riven-tpdb-mountguard` sidecar in docker-compose.yml, which clears
# the endpoint from the host namespace. See AGENTS.md.
#
# What is left for here is the other observed case: the daemon sometimes
# binds the dead endpoint through and the container starts, which reads in
# the log exactly like a healthy boot right up to the moment it dies.
# Unmounting from in here works because `rshared` propagates the unmount
# back out. Lazy, because a dead endpoint has no server left to answer a
# graceful unmount.
MOUNT_PATH=${RIVEN_FILESYSTEM_MOUNT_PATH:-/mount}

# Read the directory, never merely stat it: measured on the server, `ls -d`
# on a dead endpoint succeeds while `ls -A` on the same path returns
# ENOTCONN. `[ -d ]` and `test -e` share the stat blind spot.
if ! ls -A "$MOUNT_PATH" > /dev/null 2>&1; then
    echo "Mount path $MOUNT_PATH is unreadable (stale FUSE mount?), clearing it..."
    umount -l "$MOUNT_PATH" 2>/dev/null \
        || fusermount3 -uz "$MOUNT_PATH" 2>/dev/null \
        || fusermount -uz "$MOUNT_PATH" 2>/dev/null \
        || true
    mkdir -p "$MOUNT_PATH" 2>/dev/null || true

    if ls -A "$MOUNT_PATH" > /dev/null 2>&1; then
        echo "Mount path recovered."
    else
        # Say so loudly rather than starting into a crash loop that reads,
        # in the log, like an ordinary boot.
        echo "WARNING: $MOUNT_PATH is still unreadable. RivenVFS will fail to"
        echo "         mount. On the host: umount -l <the library bind source>"
    fi
fi

umask 002

# Define the command to run based on the DEBUG flag
if [ "${DEBUG}" != "" ]; then
    echo "Installing debugpy..."
    /riven/.venv/bin/python -m ensurepip
    /riven/.venv/bin/python -m pip install debugpy
    CMD="/riven/.venv/bin/python -m debugpy --listen 0.0.0.0:5678 src/main.py"
else
    CMD="/riven/.venv/bin/python src/main.py"
fi


echo "Container Initialization complete."
echo "Starting Riven (Backend)..."

# Execute the command
if [ "$PUID" = "0" ]; then
    exec $CMD
else
    exec su -m "$USERNAME" -c "$CMD"
fi
