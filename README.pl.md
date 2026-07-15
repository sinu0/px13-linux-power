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
wyścigi. Stan PPD zostanie odtworzony podczas odinstalowania.

## Obsługa

```bash
px13ctl state
px13ctl gaming
px13ctl confirm-cooling
px13ctl 120w
px13ctl safe
sudo ./scripts/doctor.sh
```

Przy wysokiej mocy laptop musi być otwarty i mieć drożny dolot oraz wylot
powietrza. Pełna analiza przyczyny i wyniki pomiarów są w
[`docs/FINDINGS.md`](docs/FINDINGS.md).

## Aktualizacje kernela

Po aktualizacji zrestartuj system i sprawdź:

```bash
dkms status
sudo ./scripts/doctor.sh
```

Brak modułu dla uruchomionego kernela powoduje bezpieczne nieuruchomienie
usługi, a nie zapis przypadkowych wartości WMI.

## Odinstalowanie

```bash
sudo ./uninstall.sh
```
