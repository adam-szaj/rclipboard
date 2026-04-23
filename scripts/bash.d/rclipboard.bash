
rctrl-c() {
    rclipctl put $@
}

rctrl-v() {
    rclipctl get $@
}

rclipmon() {
    bash -c "source ~/.config/rclipboard/venv/bin/activate; rclipctl monitor tui $@"
}

crealpath() {
    local R="$(realpath $@)"
    echo -n ${R} | rctrl-c --app crealpath
    echo ${R}
}

cpwd() {
    local R="$(pwd)"
    echo -n ${R} | rctrl-c -- --app cpwd
    echo ${R}
}

# prefix=r
alias ${prefix}cc='rctrl-c'
alias ${prefix}cv='rctrl-v'
unset prefix
