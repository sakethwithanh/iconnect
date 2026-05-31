# Pairing the iPhone (first run)

iOS pairs with a BT HID device through Settings → Bluetooth, but the **pointer** features live behind AssistiveTouch. Both must be on.

## 0. Linux side — start the server

```bash
sudo systemctl status bluetooth
./install.sh           # one time
./start.sh             # leave running
```

You should see `[btkbd] Ready. Pair iPhone from Settings → Bluetooth → 'iconnect'.`

## 1. iPhone — enable AssistiveTouch (for pointer)

Settings → Accessibility → Touch → AssistiveTouch → **On**. A floating dot
appears. You can dismiss/hide it later — the pointer plumbing stays active.

## 2. iPhone — pair

Settings → Bluetooth → **On**. Look for `iconnect` in "Other Devices". Tap it.

- If iOS asks for a PIN: use `bluetoothctl` on Linux to read the prompt, or rely
  on Just-Works pairing (no PIN). The default config uses NoInputNoOutput agent
  so iOS should pair without prompts.
- If pairing fails repeatedly: `bluetoothctl remove <iphone-mac>` on Linux, then
  "Forget This Device" on iPhone, then retry.

## 3. Test

After pairing, move your PC mouse — you should see a round AssistiveTouch
pointer appear and move on the iPhone screen. Click and the pointer "taps".

Type on PC keyboard — text fields should accept input.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `iconnect` doesn't appear in iOS Bluetooth list | Adapter not discoverable | `bluetoothctl discoverable on`, confirm `start.sh` ran without errors |
| Pairing fails with "Connection failed" | BlueZ input plugin still loaded | Re-run `install.sh` (writes `/etc/systemd/system/bluetooth.service.d/10-iconnect-noinput.conf`), then `sudo systemctl restart bluetooth` |
| Paired but no pointer | AssistiveTouch off | Settings → Accessibility → Touch → AssistiveTouch → On |
| Pointer appears but mouse is jittery | Both local cursor and iPhone receive events | Run `./start.sh --grab` to take exclusive control of input devices |
| Keyboard works, mouse doesn't | iOS rejected the mouse half | Re-pair after enabling AssistiveTouch first |
| Reconnect after iPhone sleep fails | iOS dropped link, BlueZ needs to listen | Server stays up; on iPhone Settings → Bluetooth, tap `iconnect` to reconnect |

## Useful commands

```bash
bluetoothctl                       # interactive shell
bluetoothctl devices               # paired devices
bluetoothctl info <MAC>            # connection state, services
sudo journalctl -u bluetooth -f    # live BlueZ log
sudo btmon                         # raw HCI trace (very verbose)
```
