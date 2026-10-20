# PX13 Linux Power

Eksperymentalne, ograniczone do modelu `HN7306WV` sterowanie mocą CPU i GPU
dla ASUS ProArt PX13 pod Linuksem. Projekt nie wgrywa VBIOS-u i nie modyfikuje
binarnie sterownika NVIDIA.

## Co faktycznie odblokowało więcej niż 100 W

Samo ASUS WMI ustawiało 95 W + 25 W, ale `nvidia-powerd` odczytywał z osobnej
odpowiedzi firmware NVPCF tylko +10 W cTGP oraz +10 W Dynamic Boost. To był
twardy limit około 100 W.

Namierzono ioctl `0xc020462a` i komendę NVPCF `0x13b`. Filtr zmienia wyłącznie
dwa pola tej odpowiedzi: cTGP `10 -> 15 W` i Dynamic Boost `10 -> 25 W`.
Sterownik otrzymuje wtedy `80 + 15 + 25 = 120 W`. Po restarcie `nvidia-powerd`
FurMark osiągnął 115,86 W. Wentylatory nie odblokowały limitu; pozwalają tylko
dłużej utrzymać moc przed dojściem GPU do celu 87°C.

## Najważniejsze zasady

- Instalator, moduł, daemon i filtr NVIDIA sprawdzają dokładny identyfikator
  DMI `ASUSTeK COMPUTER INC.` / `HN7306WV`.
- Maksymalna moc jest odczytywana z bieżącego VBIOS-u przez `nvidia-smi`.
  Profil 120 W nie pojawi się, jeśli firmware raportuje niższy limit.
- Quiet Gaming zostawia cichy profil wentylatorów ASUS Quiet i budżet WMI GPU
  z Safe, a CPU startuje z PPT 15/20/25 W i EPP `balance_performance`. Działa
  na baterii, nie wymaga potwierdzenia chłodzenia i jest przywracany po
  uśpieniu/wybudzeniu oraz restarcie daemona.
- Gaming wymaga zasilacza i otwartej pokrywy. Profil 120 W oraz ręczne
  sterowanie wymagają dodatkowo potwierdzenia chłodzenia. Potwierdzenie jest
  kasowane po uśpieniu i zmianie warunków.
- Domyślne offsety zegarów wynoszą zero; podkręcanie jest wyłącznie świadomą
  operacją użytkownika.
- DKMS automatycznie przebudowuje moduł dla nowych kerneli, jeżeli są
  zainstalowane odpowiadające im nagłówki.

## Instalacja na Arch Linux

```bash
sudo pacman -S --needed base-devel dkms python-dbus python-gobject nvidia-utils asusctl
sudo pacman -S --needed "$(cat /usr/lib/modules/$(uname -r)/pkgbase)-headers"
git clone https://github.com/sinu0/px13-linux-power.git
cd px13-linux-power
sudo ./install.sh
```

Można też użyć `sudo ./install.sh --install-deps`. Instalator wyłączy PPD,
ponieważ równoczesne zarządzanie governorami/EPP przez dwa programy powoduje
wyścigi. Stan PPD zostanie odtworzony podczas odinstalowania. Flagi
`--install-deps` i `--enable-s0ix` (opisana niżej) można łączyć w dowolnej
kolejności. Instalator ostrzega (bez przerywania instalacji), jeśli wiersz
poleceń jądra nie zawiera `amd_pstate=active`.

### Zalecany wiersz poleceń jądra

Dodaj `amd_pstate=active` do wiersza poleceń jądra (zamiast `guided`, jeśli
jest ustawiony). W trybie `guided`/`passive` pliki EPP w sysfs nie istnieją, a
governor `powersave` używany przez profil Safe w wersji 0.1.0 przypinał CPU
blisko minimalnej częstotliwości (~605 MHz) pod obciążeniem, niezależnie od
mocy CPU PPT. W trybie `active` daemon używa `powersave` + EPP; jeśli tryb
`active` jest niedostępny, w `guided`/`passive` używany jest `schedutil`,
nigdy sam `powersave`. `sudo ./scripts/doctor.sh` pokazuje bieżący stan
`amd_pstate` i ostrzega, jeśli CPU jest przypięty do minimalnej częstotliwości.
Daemon przełącza `amd_pstate` na `active` również w locie przy starcie, więc
wiersz poleceń ma znaczenie tylko przed jego uruchomieniem i po odinstalowaniu.

### Opcjonalnie: zarządzanie zasilaniem NVIDIA S0ix

Ten model obsługuje wyłącznie uśpienie s2idle, a nie sprzętowe S3. `sudo
./install.sh --enable-s0ix` instaluje plik
`/etc/modprobe.d/px13-nvidia-s0ix.conf` ustawiający
`NVreg_EnableS0ixPowerManagement=1` dla modułu NVIDIA. Jest to opcja świadomie
wyłączona domyślnie, ponieważ zmienia zachowanie dGPU przy usypianiu; po
instalacji trzeba przebudować initramfs (np. `sudo mkinitcpio -P`) i
zrestartować system. `sudo ./uninstall.sh` usuwa ten plik, jeśli istnieje, i
wypisuje tę samą uwagę o initramfs/restarcie.

## Obsługa

```bash
px13ctl state
px13ctl quiet-gaming
px13ctl gaming
px13ctl confirm-cooling
px13ctl 120w
px13ctl safe
sudo ./scripts/doctor.sh
```

Przy wysokiej mocy laptop musi być otwarty i mieć drożny dolot oraz wylot
powietrza. Pełna analiza przyczyny i wyniki pomiarów są w
[`docs/FINDINGS.md`](docs/FINDINGS.md).

### Bilans baterii w Quiet Gaming

Na zasilaniu zewnętrznym Quiet Gaming uruchamia regulator PI, który zamienia
moc PPT CPU i limit zegara GPU NVIDIA tak, by bateria delikatnie się
doładowywała podczas grania — docelowy przypadek to ładowarka USB-C PD 100 W,
która nie udźwignie pełnego poboru Gaming/120 W naraz, w odróżnieniu od
zasilacza ASUS na okrągłej wtyczce 200 W. Na baterii regulator jest
wstrzymany.

Co 5 sekund regulator wylicza przepływ mocy baterii ze zmiany `energy_now`
BAT0 w kroczącym oknie 30 sekund (potrzebuje co najmniej 15 s historii, zanim
poda odczyt, a odczyt jest wstrzymywany, gdy bateria zgłasza `Full`/`Not
charging`), i porównuje go z punktem zadanym +4 W ładowania. Nie korzysta z
`power_now`/`status`: na tym sprzęcie zaobserwowano, że firmware zgłaszał
status `Charging` z `power_now` rzędu +30..39 W, podczas gdy `energy_now`
w rzeczywistości malało — realny przepływ ok. −35 W — pod obciążeniem
grą przez USB-C; regulator ufający status/power_now podniósłby moc do 100%
dokładnie w takiej sytuacji, czyli najgorszą możliwą decyzję (patrz
[`docs/FINDINGS.md`](docs/FINDINGS.md)). Ponieważ `energy_now` ma
granularność całych µWh, odczyt z okna 30 s zmienia się skokowo, w krokach
rzędu 1,9 W, a nie płynnie. Regulator zmienia pojedynczy poziom mocy 0..100%
(start od 70%) o maksymalnie 3% na krok 5-sekundowy, więc krótkie skoki
poboru w grze (chwilowe 0 W albo nawet lekkie rozładowanie) są wchłaniane
przez baterię zamiast natychmiast ucinać zegary — regulator jest strojony
pod płynność, utrzymuje *średnią*, a nie reaguje na pojedynczą próbkę.
Powyżej 50% poziom skaluje moc ciągłą CPU (SPL)
od 12 do 25 W (SPPT = SPL + 5 W, FPPT = SPL + 10 W); poniżej 50% zamiast tego
obniża limit zegara GPU NVIDIA (`nvidia-smi --lock-gpu-clocks`) od
nieograniczonego w dół do 1200 MHz. Limit GPU jest cięty jako ostatni, dopiero
gdy CPU jest już na swoim minimum, ponieważ to on ma największe znaczenie dla
płynności klatek. Gdy bateria jest pełna albo nie ładuje się, nie ma sygnału
o dostępnym zapasie, więc poziom zostaje utrzymany na ostatniej wartości.
Ręczna zmiana mocy CPU (niestandardowa moc w `px13ctl` albo suwaki CPU w ROG
Control Center) przy aktywnym Quiet Gaming staje się nowym limitem SPL
regulatora, zamiast być nadpisywana w kolejnym kroku.

`px13ctl state` pokazuje regulator pod kluczem `battery_balance`
(`level_percent`, `cpu_spl_w`, `gpu_clock_limit_mhz`, `battery_average_w`,
`battery_net_w`) oraz wykryte źródło zasilania pod `power_source`
(`battery`, `usb-c` albo `adapter`).

### Wykrywanie USB-C PD

Daemon odróżnia ładowarkę USB-C PD od zasilacza ASUS, sprawdzając, czy jakiś
zasilacz `ucsi-source-psy-*` jest online; zasilacz na okrągłej wtyczce
pokazuje online wyłącznie `AC0`. Gaming i profil 120 W są odrzucane na
zasilaniu USB-C (`... requires the ASUS barrel adapter; USB-C PD supplies at
most 100 W`), a jeśli aktywny jest już profil wysokiej mocy i źródło zmieni
się na USB-C, daemon przełącza na Safe — tak samo jak przy odłączeniu
zasilacza czy zamknięciu pokrywy.

### Kolizja z asusd

Jeśli w `asusd` włączone są opcje `platform_profile_linked_epp` lub
`change_platform_profile_on_ac`/`change_platform_profile_on_battery`,
`asusd` może nadpisać EPP albo profil platformy tuż po ich ustawieniu przez
ten daemon (np. wymuszając EPP z powrotem na `power` w Quiet Gaming, albo na
Performance zaraz po podłączeniu zasilacza). Ustaw wszystkie trzy opcje na
`false` w `/etc/asusd/asusd.ron`, wcześniej zatrzymując `asusd` — inaczej
nadpisze ten plik przy wyjściu.

## Zachowanie na baterii

Safe automatycznie przełącza wariant po podłączeniu lub odłączeniu zasilacza:

- Na baterii: profil ASUS Quiet, EPP `power`. Daemon nie restartuje
  `nvidia-powerd` i nie sprawdza limitu mocy GPU na baterii, ponieważ firmware
  NVIDIA sam ogranicza dGPU do około 45 W i wyłącza Dynamic Boost przy zasilaniu
  z baterii; sprawdzanie limitu z AC w tym stanie dawało wcześniej fałszywy
  błąd `safe fallback failed: nvidia-powerd synchronization failed: ceiling
  45 W, expected 80 W`. dGPU celowo pozostaje uśpiony (runtime-suspended) —
  daemon unika wywołań `nvidia-smi` i `nvidia-settings` na baterii, bo każde z
  nich budzi dGPU z D3cold na ponad 20 sekund. Limity mocy z VBIOS-u są
  zapisywane w pliku stanu daemona, więc nie trzeba ich odczytywać na żywo.
- Na zasilaczu: profil ASUS Balanced, EPP `balance_power`.
- Gaming / 120 W: EPP `performance` (tak jak dotychczas, wyłącznie na AC).

## Po aktualizacjach systemu

Po każdej z tych sytuacji: aktualizacji kernela, aktualizacji sterownika
NVIDIA lub aktualizacji `asusctl`/`asusd`:

```bash
dkms status
sudo ./scripts/doctor.sh
```

Brak modułu dla uruchomionego kernela powoduje bezpieczne nieuruchomienie
usługi, a nie zapis przypadkowych wartości WMI. Doktor pokazuje też stan
`amd_pstate`, EPP, governor, profil platformy, stan zasilacza i stan dGPU —
sprawdź wszystkie linie `[FAIL]`.

Po aktualizacji sterownika NVIDIA sprawdź dodatkowo w dzienniku
`nvidia-powerd`, czy filtr NVPCF nadal działa:

```bash
journalctl -u nvidia-powerd.service | grep 'PX13 NVPCF override'
```

Brak tej linii po aktualizacji sterownika oznacza, że filtr mógł zablokować
się bezpiecznie wobec zmienionego ABI ioctl RM; moc wraca wtedy do limitu
narzuconego przez firmware (poniżej 120 W) do czasu ponownej weryfikacji
filtra.

## Odinstalowanie

```bash
sudo ./uninstall.sh
```
