# Guest assertions for command options, error paths and persistent settings.
# Every failed assertion exits before the final success marker.
mkdir -pvm750 /tmp/options/a/b || exit 3
mkdir --parents --mode=700 /tmp/options/a/b || exit 4
ls -ld /tmp/options/a/b | grep -q drwxr-x--- || exit 5
mkdir -m 888 /tmp/options/bad && exit 6
mkdir --parents=yes /tmp/options/bad && exit 7
mkdir -m && exit 8
mkdir -- /tmp/options/-dir || exit 9
cd /tmp/options || exit 10
printf 'Alpha\n\n\nbeta\nlast' > input || exit 11
cat -bsET input > visible || exit 12
printf '     1\tAlpha$\n$\n     2\tbeta$\n     3\tlast' > expected || exit 13
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 14
printf 'x\ty\n' | cat --show-tabs --show-ends > visible || exit 15
printf 'x^Iy$\n' > expected || exit 16
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 17
cat --number --number-nonblank input > visible || exit 18
cat -bn input > expected || exit 19
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 20
printf stdin | cat - > visible || exit 21
test "$(cat visible)" = stdin || exit 22
cat --bogus input && exit 23
head -n2 input > visible || exit 24
printf 'Alpha\n\n' > expected || exit 25
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 26
head --bytes=3 input > visible || exit 27
test "$(cat visible)" = Alp || exit 28
head -q -n1 input input | wc --lines > visible || exit 29
test "$(cat visible)" = '      2' || exit 30
head -v -n1 input | grep -q '==> input <==' || exit 31
tail -n1 input > visible || exit 32
test "$(cat visible)" = last || exit 33
printf last > expected || exit 34
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 35
tail --bytes=4 input > visible || exit 36
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 37
printf '1\n2\n3\n' | tail -2 > visible || exit 38
printf '2\n3\n' > expected || exit 39
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 40
head -n18446744073709551616 input && exit 41
tail -c4194305 input && exit 42
head --bytes && exit 43
printf 'Alpha\nbeta\nAlpha\n' > letters || exit 44
grep -in alpha letters > visible || exit 45
printf '1:Alpha\n3:Alpha\n' > expected || exit 46
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 47
grep -xic alpha letters > visible || exit 48
test "$(cat visible)" = 2 || exit 49
grep -Fvc Alpha letters > visible || exit 50
test "$(cat visible)" = 1 || exit 51
grep -q absent letters && exit 52
grep -l beta letters input > visible || exit 53
printf 'letters\ninput\n' > expected || exit 54
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 55
grep -hc Alpha letters letters > visible || exit 56
printf '2\n2\n' > expected || exit 57
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 58
grep -Hn beta letters > visible || exit 59
test "$(cat visible)" = letters:2:beta || exit 60
printf 'a\vb\fc\n' | wc -w > visible || exit 61
test "$(cat visible)" = '       3' || exit 62
printf x | wc --bytes - > visible || exit 63
grep -q '1 -' visible || exit 64
cp -v letters copied || exit 65
cp --no-clobber input copied || exit 66
test "$(sha256sum letters | head -c64)" = "$(sha256sum copied | head -c64)" || exit 67
cp copied copied && exit 68
test "$(sha256sum letters | head -c64)" = "$(sha256sum copied | head -c64)" || exit 69
cp -R a tree || exit 70
cp -r tree tree && exit 71
mv -n input copied || exit 72
test -f input || exit 73
mv --verbose letters visible ./-dir || exit 74
test "$(sha256sum ./-dir/letters | head -c64)" = "$(sha256sum copied | head -c64)" || exit 75
mv --no-clobber input copied || exit 76
test -f input || exit 77
rm -fv nonexistent || exit 78
rm -f || exit 79
rm --recursive --verbose tree || exit 80
test -d tree && exit 81
rm -rf / && exit 82
rm -rf . && exit 83
printf more | tee --append copied > visible || exit 84
test "$(tail -c4 copied)" = more || exit 85
tee --bogus && exit 86
basename --suffix=.txt /a/b.txt /c/d.txt > visible || exit 87
printf 'b\nd\n' > expected || exit 88
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 89
basename -z /a/b > visible || exit 90
printf 'b\0' > expected || exit 91
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 92
dirname --zero /a/b name > visible || exit 93
printf '/a\0.\0' > expected || exit 94
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 95
basename /a/b extra extra && exit 96
test "$(seq -s , -2 2 4)" = '-2,0,2,4' || exit 97
test "$(seq --separator : 3 -1 1)" = '3:2:1' || exit 98
test "$(seq 9223372036854775807 9223372036854775807)" = 9223372036854775807 || exit 99
seq 1 0 2 && exit 100
seq 9223372036854775808 && exit 101
sleep 0.001s .001 0m 0h 0d || exit 102
sleep 18446744073709551616 && exit 103
sleep -- -1 && exit 104
env -i A=one B=two > visible || exit 105
grep -xc A=one visible > expected || exit 106
test "$(cat expected)" = 1 || exit 107
env -i A=one env -u A > visible || exit 108
test -z "$(cat visible)" || exit 109
env -i --chdir=/tmp/options /bin/sh -c pwd > visible || exit 110
test "$(cat visible)" = /tmp/options || exit 111
env -i --null A=one > visible || exit 112
printf 'A=one\0' > expected || exit 113
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 114
env -i PATH=/nope cat && exit 115
env -i PATH=/bin cat expected > visible || exit 116
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 117
test "$(uname -srm)" = 'MachOS 0.9.3 x86_64' || exit 118
uname --bogus && exit 119
free -h > visible || exit 120
grep -q total visible || exit 121
df --human-readable > visible || exit 122
grep -q Filesystem visible || exit 123
du --bytes --summarize copied > visible || exit 124
printf '21\tcopied\n' > expected || exit 125
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 126
printf secret > .hidden || exit 127
ls > visible || exit 128
grep -q .hidden visible && exit 129
ls -a --color=never > visible || exit 130
grep -q .hidden visible || exit 131
ls --color=invalid && exit 132
config get hostname > visible || exit 133
test "$(cat visible)" = machos || exit 134
cp /etc/system.conf original || exit 135
cp /etc/time.conf original-time || exit 135
cp /etc/login.conf original-login || exit 135
cp /etc/network.conf original-network || exit 135
config set hostname testing || exit 136
config get hostname > visible || exit 137
test "$(cat visible)" = testing || exit 138
config set ttys 7 && exit 139
config set rtc wrong && exit 140
config set timezone nonsense && exit 141
config set network '999.1.2.3/24 1.1.1.1 8.8.8.8' && exit 142
config set missing value && exit 143
config set timezone CET-1CEST,M3.5.0,M10.5.0/3 || exit 144
config set rtc local || exit 145
config set ttys 2 || exit 146
config set autologin '' || exit 147
config set network '192.168.1.20/24 192.168.1.1 1.1.1.1' || exit 148
config get network > visible || exit 149
test "$(cat visible)" = '192.168.1.20/24 192.168.1.1 1.1.1.1' || exit 150
config set locale pl_PL.UTF-8 || exit 150
config set locale eo.UTF-8 || exit 150
test "$(config get locale)" = eo.UTF-8 || exit 150
config set locale sr_RS.UTF-8@latin || exit 150
test "$(config get locale)" = sr_RS.UTF-8@latin || exit 150
config set locale pl_PL.UTF-8 || exit 150
config set keymap uk || exit 150
config set keymap pl || exit 150
config set ntp 10.0.2.2:8123 || exit 150
test "$(config get locale)" = pl_PL.UTF-8 || exit 150
test "$(config get keymap)" = pl || exit 150
test "$(config get ntp)" = 10.0.2.2:8123 || exit 150
config set locale pl_pl.UTF-8 && exit 150
config set locale sr_RS.UTF-8@Latin && exit 150
config set keymap fr && exit 150
config set ntp 'bad/name' && exit 150
config set ntp time.example:70000 && exit 150
config list | grep -q 'hostname testing' || exit 151
cp original /etc/system.conf || exit 152
cp original-time /etc/time.conf || exit 152
cp original-login /etc/login.conf || exit 152
cp original-network /etc/network.conf || exit 152
printf ' beta\nAlpha\nalpha\n' > sortable || exit 153
sort -fbu --output=sorted sortable || exit 154
printf 'Alpha\n beta\n' > expected || exit 155
test "$(sha256sum sorted | head -c64)" = "$(sha256sum expected | head -c64)" || exit 156
sort -r -o sortable sortable || exit 157
printf 'alpha\nAlpha\n beta\n' > expected || exit 158
test "$(sha256sum sortable | head -c64)" = "$(sha256sum expected | head -c64)" || exit 159
printf '10\n-2\n999999999999999999999\n0\n' | sort -n > sorted || exit 160
printf '-2\n0\n10\n999999999999999999999\n' > expected || exit 161
test "$(sha256sum sorted | head -c64)" = "$(sha256sum expected | head -c64)" || exit 162
sort --output && exit 163
printf 'stop\cignored' > visible || exit 164
printf stop > expected || exit 165
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 166
printf '\a\b\r' | wc -c > visible || exit 167
test "$(cat visible)" = '       3' || exit 168
printf x | head -v -c1 > visible || exit 169
grep -q 'standard input' visible || exit 170
printf old > /etc/system.conf.new || exit 171
config set hostname unsafe && exit 172
test "$(cat /etc/system.conf.new)" = old || exit 173
rm /etc/system.conf.new || exit 174
config get hostname > visible || exit 175
test "$(cat visible)" = machos || exit 176
printf 'hostname duplicate\n' >> /etc/system.conf || exit 177
config set hostname final || exit 178
grep -c hostname /etc/system.conf > visible || exit 179
test "$(cat visible)" = 1 || exit 180
cp original /etc/system.conf || exit 181
# Help/version must succeed without performing command side effects.
mkdir --help > visible || exit 183
grep -q usage: visible || exit 184
mkdir --version > visible || exit 185
grep -q 0.9.3 visible || exit 186
mv --help > visible || exit 187
grep -q usage: visible || exit 188
mv --version > visible || exit 189
grep -q 0.9.3 visible || exit 190
rm --help > visible || exit 191
grep -q usage: visible || exit 192
rm --version > visible || exit 193
grep -q 0.9.3 visible || exit 194
cp --help > visible || exit 195
grep -q usage: visible || exit 196
cp --version > visible || exit 197
grep -q 0.9.3 visible || exit 198
cat --help > visible || exit 199
grep -q usage: visible || exit 200
cat --version > visible || exit 201
grep -q 0.9.3 visible || exit 202
head --help > visible || exit 203
grep -q usage: visible || exit 204
head --version > visible || exit 205
grep -q 0.9.3 visible || exit 206
tail --help > visible || exit 207
grep -q usage: visible || exit 208
tail --version > visible || exit 209
grep -q 0.9.3 visible || exit 210
grep --help > visible || exit 211
grep -q usage: visible || exit 212
grep --version > visible || exit 213
grep -q 0.9.3 visible || exit 214
wc --help > visible || exit 215
grep -q usage: visible || exit 216
wc --version > visible || exit 217
grep -q 0.9.3 visible || exit 218
tee --help > visible || exit 219
grep -q usage: visible || exit 220
tee --version > visible || exit 221
grep -q 0.9.3 visible || exit 222
basename --help > visible || exit 223
grep -q usage: visible || exit 224
basename --version > visible || exit 225
grep -q 0.9.3 visible || exit 226
dirname --help > visible || exit 227
grep -q usage: visible || exit 228
dirname --version > visible || exit 229
grep -q 0.9.3 visible || exit 230
seq --help > visible || exit 231
grep -q usage: visible || exit 232
seq --version > visible || exit 233
grep -q 0.9.3 visible || exit 234
sleep --help > visible || exit 235
grep -q usage: visible || exit 236
sleep --version > visible || exit 237
grep -q 0.9.3 visible || exit 238
env --help > visible || exit 239
grep -q usage: visible || exit 240
env --version > visible || exit 241
grep -q 0.9.3 visible || exit 242
free --help > visible || exit 243
grep -q usage: visible || exit 244
free --version > visible || exit 245
grep -q 0.9.3 visible || exit 246
df --help > visible || exit 247
grep -q usage: visible || exit 248
df --version > visible || exit 249
grep -q 0.9.3 visible || exit 250
du --help > visible || exit 251
grep -q usage: visible || exit 252
du --version > visible || exit 253
grep -q 0.9.3 visible || exit 254
ls --help > visible || exit 1
grep -q usage: visible || exit 2
ls --version > visible || exit 3
grep -q 0.9.3 visible || exit 4
uname --help > visible || exit 5
grep -q usage: visible || exit 6
uname --version > visible || exit 7
grep -q 0.9.3 visible || exit 8
config --help > visible || exit 9
grep -q usage: visible || exit 10
config --version > visible || exit 11
grep -q 0.9.3 visible || exit 12
sort --help > visible || exit 13
grep -q usage: visible || exit 14
sort --version > visible || exit 15
grep -q 0.9.3 visible || exit 16
printf --help > visible || exit 17
grep -q usage: visible || exit 18
printf --version > visible || exit 19
grep -q 0.9.3 visible || exit 20
env -i PATH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:/bin cat expected > visible || exit 21
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 22
head -c1536 /dev/zero > block || exit 23
du -h block > visible || exit 24
printf '1.5K\tblock\n' > expected || exit 25
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 26
du -m block > visible || exit 27
printf '1\tblock\n' > expected || exit 28
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 29
du -k block > visible || exit 30
printf '2\tblock\n' > expected || exit 31
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 32
head -c4194305 /dev/zero > large || exit 33
tail -c4 large > visible || exit 34
printf '\0\0\0\0' > expected || exit 35
test "$(sha256sum visible | head -c64)" = "$(sha256sum expected | head -c64)" || exit 36
sort large && exit 37
grep x large && exit 38
head -c0 input > visible || exit 39
test -z "$(cat visible)" || exit 40
tail -n0 input > visible || exit 41
test -z "$(cat visible)" || exit 42
mkdir -p search/Sub || exit 43
printf found > search/Sub/ReadMe.TXT || exit 44
test "$(find search -maxdepth 0)" = search || exit 45
test "$(find search -mindepth 1 -maxdepth 1 -type d)" = search/Sub || exit 46
test "$(find search -iname '*.txt')" = search/Sub/ReadMe.TXT || exit 47
test "$(find search -ipath '*/sub/readme.txt')" = search/Sub/ReadMe.TXT || exit 48
find search -type x && exit 49
find search -maxdepth nope && exit 50
tar --create --file=search.tar search || exit 51
tar --list --file=search.tar | grep -q 'search/Sub/ReadMe.TXT' || exit 52
tar -cfcopy.tar search || exit 53
tar -tfcopy.tar | grep -q 'search/Sub/ReadMe.TXT' || exit 54
find --help | grep -q usage: || exit 55
tar --version | grep -q 0.9.3 || exit 56
grep -r -n found search > visible || exit 57
test "$(cat visible)" = 'search/Sub/ReadMe.TXT:1:found' || exit 58
printf '%s\n' '-special' > search/Special || exit 59
grep -e -special -e absent search/Special > visible || exit 60
test "$(cat visible)" = '-special' || exit 61
grep --recursive -q found search || exit 62
echo usability: all assertions passed
