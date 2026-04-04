rctrl-c() {
    rclipctl put $@
}
rctrl-v() {
    rclipctl get $@
}
# prefix=r
alias ${prefix}cc='rctrl-c'
alias ${prefix}cv='rctrl-v'
unset prefix
