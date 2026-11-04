# ERIS Console prompt, sourced from ~/.bashrc (managed block). One line, no clock:
#   ERIS root@erisnas /path git:main ➜        exit code in red only after an error, e.g. ✘127
# Scope (PROMPT_SCOPE in /etc/eris/eris.conf): tty1 = physical console only, all = every interactive bash.
case $- in *i*) ;; *) return 0 ;; esac
[ -n "${BASH_VERSION-}" ] || return 0

__eris_scope=tty1
__eris_ctty=/dev/tty1
if [ -r /etc/eris/eris.conf ]; then
  while IFS='=' read -r __eris_k __eris_v; do
    __eris_v=${__eris_v%%#*}
    __eris_v=${__eris_v%"${__eris_v##*[![:space:]]}"}
    __eris_v=${__eris_v#\"}
    __eris_v=${__eris_v%\"}
    case $__eris_k in
      PROMPT_SCOPE) __eris_scope=$__eris_v ;;
      CONSOLE_TTY) __eris_ctty=$__eris_v ;;
    esac
  done < /etc/eris/eris.conf
  unset __eris_k __eris_v
fi
__eris_tty=$(tty 2>/dev/null)
if [ "$__eris_scope" != all ] && [ "$__eris_tty" != "$__eris_ctty" ]; then
  unset __eris_scope __eris_ctty __eris_tty
  return 0
fi

# Glyphs: the physical console only gets what its font can draw (ERIS writes /run/eris/prompt.env).
__eris_arrow='>'
__eris_err='x'
__eris_fsapps=''
__eris_envfile=''
if [ "$__eris_tty" = "$__eris_ctty" ] || [ "${TERM-}" = linux ]; then
  __eris_envfile=/run/eris/prompt.env
else
  case "${LC_ALL:-${LC_CTYPE:-${LANG-}}}" in
    *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*) __eris_arrow='➜' __eris_err='✘' ;;
  esac
fi
if [ -r /run/eris/prompt.env ]; then
  while IFS='=' read -r __eris_k __eris_v; do
    __eris_v=${__eris_v#\'}
    __eris_v=${__eris_v%\'}
    case $__eris_k in
      ERIS_ARROW) [ -n "$__eris_envfile" ] && __eris_arrow=$__eris_v ;;
      ERIS_ERRMARK) [ -n "$__eris_envfile" ] && __eris_err=$__eris_v ;;
      ERIS_FULLSCREEN) __eris_fsapps=" $__eris_v " ;;
    esac
  done < /run/eris/prompt.env
  unset __eris_k __eris_v
fi
# Files of a dashboard running on this terminal (see erisd.py), used by the preexec hook below.
__eris_state=/run/eris/dashboard-${__eris_tty#/dev/}.state
__eris_handoff=/run/eris/handoff-${__eris_tty#/dev/}
unset __eris_scope __eris_ctty __eris_tty __eris_envfile

# Current git branch without running git (it may not be installed): read .git/HEAD upwards.
__eris_git() {
  __eris_br=
  local d=$PWD h='' g=''
  while [ -n "$d" ]; do
    if [ -f "$d/.git/HEAD" ]; then
      read -r h < "$d/.git/HEAD"
      break
    elif [ -f "$d/.git" ]; then
      read -r g < "$d/.git"
      g=${g#gitdir: }
      [ "${g#/}" = "$g" ] && g=$d/$g
      [ -f "$g/HEAD" ] && read -r h < "$g/HEAD"
      break
    fi
    [ "$d" = / ] && return 0
    d=${d%/*}
    [ -z "$d" ] && d=/
  done
  [ -n "$h" ] || return 0
  case $h in
    "ref: refs/heads/"*) h=${h#ref: refs/heads/} ;;
    *) h=${h:0:7} ;;
  esac
  h=${h//[^A-Za-z0-9._\/-]/_}   # nothing that PS1 could expand or that breaks the line
  __eris_br=${h:0:32}
}

__eris_prompt() {
  local ec=$? e='' g=''
  __eris_git
  [ -n "$__eris_br" ] && g=" \[\e[0;1;33m\]git:${__eris_br}"
  [ "$ec" -ne 0 ] && e=" \[\e[0;1;31m\]${__eris_err}${ec}"
  PS1="\[\e[0;1;35m\]ERIS \[\e[0;35m\]\u\[\e[0;1;30m\]@\[\e[0;1;34m\]\h \[\e[0;1;32m\]\w${g}${e} \[\e[0;1;36m\]${__eris_arrow}\[\e[0m\] "
  PS2="\[\e[0;1;30m\]${__eris_arrow}\[\e[0m\] "
  __eris_armed=1
  return "$ec"
}

# While the ERIS dashboard owns the top of this terminal, give a full-screen program (vim, htop, ...)
# the whole screen *before* it starts drawing: ncurses programs do not repaint on a same-size SIGWINCH.
# Only the first command of each line typed at the prompt is looked at; the dashboard comes back by itself.
__eris_preexec() {
  [ -n "${__eris_armed-}" ] || return 0
  __eris_armed=
  [ -n "${COMP_LINE-}" ] && return 0
  [ -e "$__eris_state" ] || return 0
  local w=${BASH_COMMAND%% *}
  case $w in
    sudo | env | nice | time | exec | command | nohup | ionice) w=${BASH_COMMAND#* } && w=${w%% *} ;;
  esac
  case "$__eris_fsapps" in
    *" ${w##*/} "*) ;;
    *) return 0 ;;
  esac
  : > "$__eris_handoff" 2> /dev/null
  printf '\033[?6l\033[r\033[0m\033[H\033[2J'
}

# Alt+F: dashboard full screen on/off (root only: eris changes /etc/eris/eris.conf).
__eris_full() {
  [ "$(id -u)" = 0 ] && command -v eris > /dev/null && eris full toggle > /dev/null 2>&1
}
bind -x '"\ef": __eris_full' 2> /dev/null

# Run first so $? is still the last command's status; drop the hook of the previous ERIS version.
if [ -z "$(trap -p DEBUG)" ]; then
  trap '__eris_preexec' DEBUG
fi
unset -f __eris_ps1 2>/dev/null
PROMPT_COMMAND=${PROMPT_COMMAND//__eris_ps1; /}
PROMPT_COMMAND=${PROMPT_COMMAND//__eris_ps1/}
case "${PROMPT_COMMAND-}" in
  *__eris_prompt*) ;;
  *) PROMPT_COMMAND="__eris_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}" ;;
esac
