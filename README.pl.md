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

- Moduł i filtr NVIDIA sprawdzają dokładny identyfikator DMI
  `ASUSTeK COMPUTER INC.` / `HN7306WV`, a instalator i daemon wymagają
  pasującego pliku modelu.
- Maksymalna moc jest odczytywana z bieżącego VBIOS-u przez NVML. Daemon
  używa tylko profili, które się w nim mieszczą.
- Limity podążają za profilem platformy, jak tryby w G-Helperze: quiet daje
  Quiet Gaming, balanced daje Safe, a performance daje Gaming (wymaga
  zasilania zewnętrznego). Własne zasady, np. profil 120 W dla zmodyfikowanego
  VBIOS-u albo wymóg otwartej pokrywy, są opcjonalną konfiguracją (patrz
  niżej).
- Domyślne offsety zegarów wynoszą zero; podkręcanie jest wyłącznie świadomą
  operacją użytkownika.
- DKMS automatycznie przebudowuje moduł dla nowych kerneli, jeżeli są
  zainstalowane odpowiadające im nagłówki.

## Instalacja na Arch Linux

```bash
sudo pacman -S --needed base-devel dkms python-dbus python-gobject python-nvidia-ml-py nvidia-utils asusctl
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

Tryb wybiera się profilem platformy: Fn+F5, listą profili w ROG Control Center
albo `asusctl profile set`. Automatyczne przełączanie profilu przez asusd na
zasilaczu i baterii („Throttle policy on AC/battery” w ROG Control Center)
zmienia również limity.

```bash
px13ctl state            # tryb, profil, limity, telemetria
px13ctl capabilities     # tryby i zakresy do strojenia
px13ctl reset            # cofa ręczne zmiany aktywnego profilu
px13ctl confirm-cooling
sudo ./scripts/doctor.sh
```

Plik modelu [`daemon/models/hn7306wv.toml`](daemon/models/hn7306wv.toml)
podaje dla każdego trybu listę profili; stosowany jest pierwszy, którego
warunki są spełnione, a gdy żaden — Safe. Daemon ocenia to od nowa przy każdej
zmianie trybu, źródła zasilania, pokrywy i potwierdzenia chłodzenia: po
odłączeniu zasilacza w trybie performance przechodzi na Safe, a po
podłączeniu wraca do Gaming. Safe jest też utrzymywany podczas uśpienia i przy
zatrzymaniu usługi.

Ręczne strojenie (suwaki w ROG Control Center) działa w każdym profilu poza
Safe, na zasilaniu zewnętrznym. Zmiany są pamiętane per profil, także po
restarcie; `px13ctl reset` albo przyciski resetu suwaków je cofają. Żądania
wracają od razu, a wolny restart `nvidia-powerd` odbywa się w tle i łączy
kilka szybkich zmian w jedną.

### Własne profile

Pliki `/etc/px13-powerd/profiles.d/*.toml` mogą zmieniać tryby, dodawać
profile, zmieniać dowolny klucz istniejącego i ustawiać warunki ręcznego
strojenia. Są sprawdzane względem zakresów firmware modelu przy starcie oraz
przez `sudo /usr/local/lib/px13-powerd/px13_powerd.py --check`. Dostępne
warunki: `ac` (dowolne zasilanie zewnętrzne), `adapter` (zasilacz, nie
USB-C), `lid_open` (otwarta pokrywa) i `cooling_confirmation` (potwierdzenie
chłodzenia w RCC albo `px13ctl confirm-cooling`). Potwierdzenie wymaga
zasilania i otwartej pokrywy i jest kasowane, gdy któregoś zabraknie, po
uśpieniu oraz po restarcie usługi.

[`examples/profiles.d/120w.toml`](examples/profiles.d/120w.toml) to pełny
przykład: tryb performance używa profilu 120 W po potwierdzeniu chłodzenia, a
w przeciwnym razie Gaming, oba tylko na zasilaczu z otwartą pokrywą; ręczne
strojenie wymaga tego samego. Profil 120 W jest pomijany, gdy VBIOS raportuje
mniej niż 120 W.

```bash
sudo install -Dm644 examples/profiles.d/120w.toml /etc/px13-powerd/profiles.d/120w.toml
sudo systemctl restart px13-powerd
```

Przy wysokiej mocy laptop musi być otwarty i mieć drożny dolot oraz wylot
powietrza. Pełna analiza przyczyny i wyniki pomiarów są w
[`docs/FINDINGS.md`](docs/FINDINGS.md).

### ROG Control Center

Kontrolki pojawiają się w ROG Control Center z małym forkiem asusctl, który
dodaje obsługę zewnętrznego providera; patrz [`rcc/`](rcc/). CLI i backend
D-Bus działają bez niego.

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
Ręczna zmiana mocy CPU (suwaki CPU w ROG Control Center) przy aktywnym Quiet
Gaming staje się nowym limitem SPL regulatora, zamiast być nadpisywana w
kolejnym kroku. Regulator zarządza limitem zegara GPU, więc ręczne kontrolki
zegarów są w Quiet Gaming wyłączone.

`px13ctl state` pokazuje regulator pod kluczem `battery_balance`
(`level_percent`, `cpu_spl_w`, `gpu_clock_limit_mhz`, `battery_net_w`) oraz wykryte źródło zasilania pod `power_source`
(`battery`, `usb-c` albo `adapter`).

### Wykrywanie USB-C PD

Daemon odróżnia ładowarkę USB-C PD od zasilacza ASUS, sprawdzając, czy jakiś
zasilacz `ucsi-source-psy-*` jest online; zasilacz na okrągłej wtyczce
pokazuje online wyłącznie zasilacz typu Mains. Profil wymagający `adapter`
jest pomijany na zasilaniu USB-C. Ładowarka USB-C PD 100 W nie udźwignie
Gaming (w testach bateria traciła ok. 40 W), dlatego przykładowe profile
własne wymagają zasilacza, a do ładowania przez USB-C służy tryb quiet z
bilansem baterii.

### Kolizja z asusd

Opcja `platform_profile_linked_epp` w asusd ustawia EPP dla każdego profilu
platformy, co koliduje z EPP profili tego daemona (np. wymusza `power` w
Quiet Gaming). Wyłącz ją: „Link EPP to throttle policy” w ROG Control Center
albo `platform_profile_linked_epp: false` w `/etc/asusd/asusd.ron` przy
zatrzymanym asusd, bo nadpisuje ten plik przy wyjściu. `scripts/doctor.sh` to
sprawdza. Przełączanie profilu przez asusd na zasilaczu i baterii jest
mile widziane: limity za nim podążają.

## Zachowanie na baterii

Na baterii daemon nie restartuje `nvidia-powerd` i nie sprawdza limitu mocy
GPU: firmware NVIDIA sam ogranicza dGPU do ok. 40-45 W i wyłącza Dynamic
Boost. Sprawdzanie limitu z AC dawało wcześniej fałszywy błąd
`nvidia-powerd synchronization failed: ceiling 45 W, expected 80 W`. dGPU
celowo pozostaje uśpiony: daemon nie wywołuje NVML, gdy GPU śpi, bo każdy
klient NVML budzi je z D3cold na ponad 20 sekund, a limity VBIOS-u są
zapisane w pliku stanu.

Safe na baterii obniża tylko EPP do `power`. Cichsze ustawienia firmware na
baterii daje przełączanie przez asusd na tryb quiet na baterii.

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

Zatrzymanie usługi przywraca Safe. Własne profile w `/etc/px13-powerd`
zostają.

## Upstream

Sterownik `asus-armoury` w jądrze używa tych samych identyfikatorów WMI co
moduł z tego repozytorium, ale nie ma wpisu z limitami dla `HN7306WV`. Patch
dodający ten wpis jest w [`upstream/`](upstream/) razem z instrukcją testu i
wysyłki; po jego przyjęciu limity CPU i GPU obsłużą standardowe asusd i ROG
Control Center.

## Dodawanie modelu

Daemon działa tylko z zweryfikowanym plikiem modelu. Nowa płyta wymaga wpisu
DMI w tabeli `px13_dmi_table` modułu (po sprawdzeniu, że firmware używa tych
samych identyfikatorów WMI), pliku modelu w `daemon/models/` z zakresami
zmierzonymi na tej płycie, `make check` i pomiaru przez `px13_benchmark.py`.

## Rozwój

`make check` sprawdza składnię, uruchamia testy jednostkowe i buduje filtr
NVPCF oraz moduł jądra. Daemon jest podzielony na ładowanie konfiguracji
(`config.py`), cienkie interfejsy sprzętu (`hardware.py` oraz `nvidia.py` na
NVML), regulator baterii (`balance.py`) i politykę (`controller.py`), która
nie wie nic o D-Bus. Polityka trzyma jeden pożądany stan: żądania go zmieniają
i od razu wykonują szybkie zapisy, a jeden wątek roboczy wykonuje wolne kroki,
przy czym nowsza praca zastępuje starszą. Testy uruchamiają politykę na
symulowanym drzewie sysfs.
