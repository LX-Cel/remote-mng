#!/bin/sh
# Install or update remote-mng from a verified public GitHub release.
# No Python, uv, jq, Git checkout, administrator access or GitHub login required.
set -eu
LC_ALL=C
export LC_ALL

fail() { printf 'remote-mng: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'USAGE'
Usage: sh install.sh [--preview] [--version vX.Y.Z] [advanced options]

Install or safely update remote-mng. During preview, a compatible preview may be selected.
  --preview               Include reviewed GitHub prereleases in selection
  --version vX.Y.Z        Install one exact published release
  --install-dir DIRECTORY Personal program directory
  --home DIRECTORY        Personal remote-mng state directory
  --claude-dir DIRECTORY  Claude Code configuration directory
  --repository OWNER/REPO Release repository (default LX-Cel/remote-mng)
  --no-start              Install without starting the local manager
USAGE
}
repository=LX-Cel/remote-mng
version=''
preview=no
start=yes
# Omitted context is intentional: the candidate reuses the existing Skill
# binding and installation record before choosing fresh-install defaults.
install_dir=''
state_home=''
claude_dir=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --preview) preview=yes; shift ;;
    --no-start) start=no; shift ;;
    --version|--install-dir|--home|--claude-dir|--repository)
      [ "$#" -ge 2 ] && [ -n "$2" ] || fail "$1 requires a value."
      case "$1" in
        --version) version=$2 ;;
        --install-dir) install_dir=$2 ;;
        --home) state_home=$2 ;;
        --claude-dir) claude_dir=$2 ;;
        --repository) repository=$2 ;;
      esac
      shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; fail "Unknown option: $1" ;;
  esac
done
printf '%s\n' "$repository" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$' || fail 'Use a GitHub OWNER/REPO repository.'
if [ -n "$version" ]; then
  printf '%s\n' "$version" | grep -Eq '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' || fail 'Use a complete release version such as v0.5.0.'
  # Releases older than 0.5 have no safe bootstrap command. Refuse before
  # downloading metadata or executing any candidate from an old release.
  case "$version" in
    v0.[0-4].*) fail 'The easy installer requires v0.5.0 or newer. Use the legacy instructions for older versions.' ;;
  esac
fi
[ "$(uname -s)" = Linux ] || fail 'This installer supports Linux and WSL. On Windows use install.ps1.'
case "$(uname -m)" in
  x86_64|amd64) platform=linux-x86_64 ;;
  *) fail 'This release installer currently supports Linux/WSL x86_64 only.' ;;
esac
for dependency in curl awk unzip sha256sum mktemp; do
  command -v "$dependency" >/dev/null 2>&1 || fail "Missing $dependency. On Ubuntu/WSL install the standard packages with: sudo apt install curl unzip coreutils mawk"
done
stage=$(mktemp -d "${TMPDIR:-/tmp}/remote-mng-install.XXXXXXXX") || fail 'Could not create a private temporary directory.'
keep_logs=no
cleanup() {
  [ "$keep_logs" = no ] || return 0
  case "$stage" in "${TMPDIR:-/tmp}"/remote-mng-install.*) rm -rf -- "$stage" ;; esac
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
download() {
  # HTTPS is mandatory, including redirects. No authentication is needed.
  curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --connect-timeout 15 --max-time 300 --retry 2 --max-filesize "${3:-1073741824}" \
    --output "$2" "$1" || fail "Could not download $1. Check your network and GitHub access, then run the installer again."
}
metadata_url="https://api.github.com/repos/$repository/releases/latest"
if [ "$preview" = yes ]; then metadata_url="https://api.github.com/repos/$repository/releases?per_page=100"; fi
if [ -n "$version" ]; then metadata_url="https://api.github.com/repos/$repository/releases/tags/$version"; fi
printf '%s\n' 'Finding a compatible remote-mng release...'
download "$metadata_url" "$stage/release.json" 8388608

# A bounded JSON parser, not grep over JSON: release notes may contain fake field
# names, quotes and escapes. Values are data only; nothing is evaluated as shell.
# GitHub release tags currently supported by the program are vMAJOR.MINOR.PATCH.
cat > "$stage/parser.awk" <<'JSON_PARSER'
function die() { invalid=1; exit 1 }
function ws() { while (substr(data,pos,1) ~ /[ \t\r\n]/ && pos<=length(data)) pos++ }
function hex(c) { return index("0123456789abcdef",tolower(c))-1 }
function string(    c,out,e,h,n,i) {
  if (substr(data,pos++,1)!="\"") die()
  out=""
  while (pos<=length(data)) {
    c=substr(data,pos++,1)
    if (c=="\"") return out
    if (c ~ /[[:cntrl:]]/) die()
    if (c=="\\") {
      e=substr(data,pos++,1)
      if (e=="\"" || e=="\\" || e=="/") c=e
      else if (e=="b") c=sprintf("%c",8)
      else if (e=="f") c=sprintf("%c",12)
      else if (e=="n") c="\n"
      else if (e=="r") c="\r"
      else if (e=="t") c="\t"
      else if (e=="u") {
        h=substr(data,pos,4); if (length(h)!=4 || h ~ /[^0-9A-Fa-f]/) die()
        n=0; for(i=1;i<=4;i++) n=n*16+hex(substr(h,i,1)); pos+=4
        # Non-ASCII text is irrelevant to release selection. ASCII escapes in
        # keys are decoded so duplicate keys cannot hide behind an escape.
        c=(n>=32 && n<=126)?sprintf("%c",n):"?"
      } else die()
    }
    out=out c
  }
  die()
}
function save(path,kind,text,    p,n,r,a) {
  if(operation=="result") {
    if(path=="") result_root=kind
    if(path=="/state" && kind=="string") result_state=text
    if(path=="/version" && kind=="string") result_version=text
    if(path=="/current_version" && kind=="string") result_current_version=text
    if((path=="/message" || path=="/error/message") && kind=="string") result_message=text
    if(path=="/advice" && kind=="string") result_advice=text
    return
  }
  p=path
  if (root_array) {
    sub(/^\//,"",p); n=index(p,"/")
    if (!n) {if(p ~ /^[0-9]+$/) types[p+1,"root"]=kind; return}
    r=substr(p,1,n-1)+1; p=substr(p,n)
  }
  else r=1
  if (p=="") types[r,"root"]=kind
  else if (p=="/assets") types[r,"/assets"]=kind
  else if(p ~ /^\/assets\/[0-9]+$/) {
    split(p,a,"/"); types[r,"asset",a[3]]=kind
  } else if (p=="/tag_name" || p=="/draft" || p=="/prerelease") {
    fields[r,p]=text; types[r,p]=kind; if(r>releases) releases=r
  } else if (p ~ /^\/assets\/[0-9]+\/(name|digest)$/) {
    n=split(p,a,"/"); fields[r,"asset",a[3],a[4]]=text; types[r,"asset",a[3],a[4]]=kind
    if(a[3]+1>assets[r]) assets[r]=a[3]+1
  }
}
function value(path,depth,    c,key,k,i,start,token) {
  if (depth>64) die()
  ws(); c=substr(data,pos,1)
  if(c=="{") {
    save(path,"object","")
    pos++; ws(); if(substr(data,pos,1)=="}") {pos++; return}
    while(1) {
      ws(); if(substr(data,pos,1)!="\"") die(); key=string(); k=path SUBSEP key
      if(seen[k]++) die()
      ws(); if(substr(data,pos++,1)!=":") die(); value(path "/" key,depth+1)
      ws(); c=substr(data,pos++,1); if(c=="}") return; if(c!=",") die()
    }
  }
  if(c=="[") {
    save(path,"array","")
    pos++; ws(); if(substr(data,pos,1)=="]") {if(path=="") root_items=0; pos++; return}
    i=0; while(1) {
      value(path "/" i++,depth+1); ws(); c=substr(data,pos++,1)
      if(c=="]") {if(path=="") root_items=i; return}; if(c!=",") die()
    }
  }
  if(c=="\"") {save(path,"string",string()); return}
  start=pos
  while(pos<=length(data) && substr(data,pos,1) !~ /[ \t\r\n,}\]]/) pos++
  token=substr(data,start,pos-start)
  if(token=="true" || token=="false") save(path,"boolean",token)
  else if(token=="null") save(path,"null",token)
  else if(token ~ /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?$/) save(path,"number",token)
  else die()
}
function newer(left,right,    a,b,i) {
  sub(/^v/,"",left); sub(/^v/,"",right); split(left,a,"."); split(right,b,".")
  for(i=1;i<=3;i++) {
    if(length(a[i])>length(b[i]))return 1; if(length(a[i])<length(b[i]))return 0
    if(("x" a[i])>("x" b[i]))return 1; if(("x" a[i])<("x" b[i]))return 0
  }
  return 0
}
{ bytes+=length($0)+1; if(bytes>8388608) die(); data=data $0 "\n" }
END {
  if(invalid) exit 1
  pos=1; ws(); root_array=(substr(data,pos,1)=="[")
  if(!root_array && substr(data,pos,1)!="{") die()
  value("",0); ws(); if(pos<=length(data)) die()
  if(operation=="release" && counts_path!="") print root_items+0 > counts_path
  if(expected_array=="yes" && !root_array) die()
  if(operation=="result") {
    if(result_root!="object") die()
    gsub(/[[:cntrl:]]/," ",result_message); gsub(/[[:cntrl:]]/," ",result_advice)
    actual_version=(result_version!="")?result_version:result_current_version
    if(actual_version!="" && actual_version !~ /^[0-9]+\.[0-9]+\.[0-9]+$/) die()
    print result_state; print substr(result_message,1,800); print substr(result_advice,1,800); print actual_version
    exit
  }
  best=0
  for(r=1;r<=releases;r++) {
    tag=fields[r,"/tag_name"]
    if(types[r,"root"]!="object") die()
    if(types[r,"/tag_name"]!="string" || tag !~ /^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$/) continue
    if(types[r,"/draft"]!="boolean" || types[r,"/prerelease"]!="boolean") die()
    if(newer("v0.5.0",tag)) continue
    if(fields[r,"/draft"]=="true" || (mode!="yes" && requested=="" && fields[r,"/prerelease"]=="true")) continue
    if(requested!="" && requested!=tag) continue
    if(!best || newer(tag,fields[best,"/tag_name"])) best=r
  }
  if(!best) exit 3
  tag=fields[best,"/tag_name"]; name="remote-mng-" substr(tag,2) "-" platform ".zip"
  if(types[best,"/assets"]!="array") die()
  matches=0; checksum=0; digest=""
  for(i=0;i<assets[best];i++) {
    if(types[best,"asset",i]!="object") die()
    if(types[best,"asset",i,"name"]!="string") die()
    if(fields[best,"asset",i,"name"]==name) {
      matches++; digest=fields[best,"asset",i,"digest"]
      if(types[best,"asset",i,"digest"]=="null") digest=""
      else if(digest!="" && types[best,"asset",i,"digest"]!="string") die()
    }
    if(fields[best,"asset",i,"name"]==name ".sha256") checksum++
  }
  if(matches!=1 || checksum>1) die()
  if(digest !~ /^sha256:[0-9a-fA-F]+$/ || length(digest)!=71) {
    if(checksum!=1) die(); digest="checksum-file"
  }
  print tag; print name; print digest; print fields[best,"/prerelease"]
}
JSON_PARSER
select_release() {
  awk -v operation=release -v mode="$preview" -v requested="$version" -v platform="$platform" \
    -v expected_array="${expect_array:-no}" -v counts_path="$stage/release-count" \
    -f "$stage/parser.awk" "$stage/release.json" > "$stage/selected"
}
select_list() {
  # GitHub lists by publication time, not semantic version. Inspect bounded
  # pages before deciding so a newer numeric tag on an older page is not lost.
  page=1
  expect_array=yes
  rm -f -- "$stage/winner"
  while :; do
    if [ "$page" -gt 1 ]; then
      download "https://api.github.com/repos/$repository/releases?per_page=100&page=$page" "$stage/release.json" 8388608
    fi
    page_exit=0
    select_release || page_exit=$?
    case "$page_exit" in 0|3) ;; *) expect_array=no; return "$page_exit" ;; esac
    if [ "$page_exit" -eq 0 ]; then
      IFS= read -r contender < "$stage/selected"
      if [ ! -f "$stage/winner" ]; then cp "$stage/selected" "$stage/winner"
      else
        IFS= read -r winner < "$stage/winner"
        if awk -v left="$contender" -v right="$winner" '
          BEGIN {
            sub(/^v/,"",left);sub(/^v/,"",right);split(left,a,".");split(right,b,".")
            for(i=1;i<=3;i++) {
              if(length(a[i])!=length(b[i])) exit !(length(a[i])>length(b[i]))
              if(("x" a[i])!=("x" b[i])) exit !(("x" a[i])>("x" b[i]))
            }
            exit 1
          }'; then cp "$stage/selected" "$stage/winner"; fi
      fi
    fi
    IFS= read -r page_items < "$stage/release-count"
    [ "$page_items" -ge 100 ] || break
    [ "$page" -lt 10 ] || fail 'Too many releases to select safely. Use --version with one exact compatible release.'
    page=$((page+1))
  done
  expect_array=no
  [ -f "$stage/winner" ] || return 3
  mv "$stage/winner" "$stage/selected"
}
selection_exit=0
if [ "$preview" = yes ] && [ -z "$version" ]; then select_list || selection_exit=$?
else select_release || selection_exit=$?; fi
if [ "$selection_exit" -ne 0 ]; then
  if [ "$selection_exit" -eq 3 ] && [ -z "$version" ] && [ "$preview" = no ]; then
    # The old stable 0.3/0.4 packages predate this installer. During preview,
    # discover a compatible candidate instead of downloading an unusable ZIP.
    preview=yes
    download "https://api.github.com/repos/$repository/releases?per_page=100" "$stage/release.json" 8388608
    if ! select_list; then
      fail 'No compatible easy-installer release (v0.5.0 or newer) has been published yet. Use the published legacy installation instructions for now.'
    fi
    printf '%s\n' 'remote-mng is currently in preview; selecting the newest compatible preview release.'
  elif [ "$selection_exit" -eq 3 ]; then
    fail 'No compatible easy-installer release (v0.5.0 or newer) has been published yet.'
  else
    fail 'The release metadata is invalid, unavailable for this platform, or has no unique checksum. No program was installed.'
  fi
fi
{ IFS= read -r selected_tag; IFS= read -r asset; IFS= read -r digest; IFS= read -r selected_preview; } < "$stage/selected"
if [ "$selected_preview" = true ]; then preview=yes; fi
base_url="https://github.com/$repository/releases/download/$selected_tag"
if [ "$digest" = checksum-file ]; then
  download "$base_url/$asset.sha256" "$stage/checksum" 2048
  expected=$(awk -v name="$asset" '
    NR==1 && NF==2 && length($1)==64 && $1 !~ /[^0-9a-fA-F]/ {n=$2; sub(/^\*/,"",n); if(n==name) result=tolower($1); else bad=1; next}
    {bad=1} END {if(NR!=1 || bad || result=="") exit 1; print result}
  ' "$stage/checksum") || fail 'The release checksum file does not match the selected package.'
else
  expected=$(printf '%s' "${digest#sha256:}" | tr A-F a-f)
fi
printf 'Downloading remote-mng %s...\n' "${selected_tag#v}"
download "$base_url/$asset" "$stage/package.zip"
actual=$(sha256sum -- "$stage/package.zip"); actual=${actual%% *}
[ "$actual" = "$expected" ] || fail 'Package checksum mismatch. No downloaded program was executed.'
unzip -Z1 "$stage/package.zip" > "$stage/entries" || fail 'The downloaded package is not a valid ZIP.'
[ -s "$stage/entries" ] || fail 'The downloaded package is empty.'
while IFS= read -r entry; do
  case "$entry" in
    ''|/*|*\\*|*../*|../*|*/..|..|*:*) fail 'Unsafe archive path. No downloaded program was executed.' ;;
  esac
  # Newlines/control characters cannot be valid release paths. A split name is
  # also rejected because root entries are constrained before extraction.
  printf '%s' "$entry" | grep -q '[[:cntrl:]]' && fail 'Unsafe archive path.'
done < "$stage/entries"
unzip -Z -l "$stage/package.zip" > "$stage/zip-info" || fail 'Could not inspect the ZIP file.'
if grep -Eq '^[lbcps]' "$stage/zip-info"; then fail 'Archive links and special files are forbidden.'; fi
unzip -q "$stage/package.zip" -d "$stage/payload" || fail 'Could not unpack the verified release.'
[ -f "$stage/payload/rmg" ] && [ ! -L "$stage/payload/rmg" ] || fail 'The release has no program at its archive root.'
chmod u+x "$stage/payload/rmg"
if ! "$stage/payload/rmg" bootstrap --help >/dev/null 2>&1; then
  fail "Release $selected_tag predates the easy installer. Choose a release that supports it with --version, or use the legacy release instructions. The existing installation was not stopped."
fi
set -- --json
if [ -n "$state_home" ]; then set -- "$@" --home "$state_home"; fi
set -- "$@" bootstrap "$stage/package.zip" --sha256 "$expected" --repository "$repository" --tag "$selected_tag"
if [ -n "$install_dir" ]; then set -- "$@" --install-dir "$install_dir"; fi
if [ -n "$claude_dir" ]; then set -- "$@" --claude-dir "$claude_dir"; fi
if [ "$start" = no ]; then set -- "$@" --no-start; fi
if [ "$preview" = yes ]; then set -- "$@" --preview; fi
printf '%s\n' 'Installing remote-mng and the Claude Code Skill; this may take a moment...'
bootstrap_exit=0
"$stage/payload/rmg" "$@" > "$stage/bootstrap-result.json" 2> "$stage/bootstrap-error.log" || bootstrap_exit=$?
if ! awk -v operation=result -f "$stage/parser.awk" "$stage/bootstrap-result.json" > "$stage/outcome"; then
  keep_logs=yes
  printf 'Installation did not return a verified result. Details: %s\n' "$stage/bootstrap-result.json" >&2
  fail 'The installation may need attention. Ask your Agent to inspect the retained logs before retrying.'
fi
{ IFS= read -r outcome_state; IFS= read -r outcome_message; IFS= read -r outcome_advice; IFS= read -r outcome_version; } < "$stage/outcome"
if [ "$bootstrap_exit" -ne 0 ] || [ "$outcome_state" != succeeded ]; then
  keep_logs=yes
  [ -z "$outcome_message" ] || printf 'remote-mng: %s\n' "$outcome_message" >&2
  [ -z "$outcome_advice" ] || printf '%s\n' "$outcome_advice" >&2
  printf 'Diagnostic logs: %s\n' "$stage" >&2
  fail 'Installation or update needs attention. Existing sessions were not forcibly stopped. Ask your Agent to inspect these logs and resolve the reported blocker.'
fi
printf '\nremote-mng %s is ready. Open a new Claude Code session to load the updated Skill.\n' "${outcome_version:-${selected_tag#v}}"
