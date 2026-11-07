import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Pango from 'gi://Pango';
import St from 'gi://St';

import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {QuickMenuToggle, SystemIndicator} from 'resource:///org/gnome/shell/ui/quickSettings.js';

// The Bluetooth/ANCS work lives in the iphone-notify daemon (a systemd user
// service). This extension is only its UI, talking to it over the session bus.
const BUS_NAME = 'io.github.arjun.iPhoneNotify';
const OBJ_PATH = '/io/github/arjun/iPhoneNotify';
const UNIT = 'iphone-notify.service';
const RECENT_SHOWN = 5;

const DaemonProxy = Gio.DBusProxy.makeProxyWrapper(`
<node>
  <interface name="${BUS_NAME}">
    <method name="GetState">
      <arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="b" direction="out"/>
    </method>
    <method name="GetRecent"><arg type="a(sssx)" direction="out"/></method>
    <method name="StartPairing"/>
    <method name="StopPairing"/>
    <signal name="StateChanged"><arg type="s"/><arg type="s"/><arg type="b"/></signal>
    <signal name="PairingCode"><arg type="u"/></signal>
    <signal name="NotificationReceived">
      <arg type="s"/><arg type="s"/><arg type="s"/><arg type="x"/>
    </signal>
  </interface>
</node>`);

const PHONE_ICON = new Gio.ThemedIcon({
    names: ['phone-apple-iphone-symbolic', 'phone-symbolic'],
});

function fmtTime(ts) {
    const dt = GLib.DateTime.new_from_unix_local(Number(ts));
    const now = GLib.DateTime.new_now_local();
    return dt.format(dt.get_day_of_year() === now.get_day_of_year() ? '%H:%M' : '%b %e');
}

const IPhoneToggle = GObject.registerClass(
class IPhoneToggle extends QuickMenuToggle {
    _init(indicator) {
        super._init({title: 'iPhone', gicon: PHONE_ICON, toggleMode: false});
        this._indicator = indicator;
        this._state = {running: false, status: 'waiting', device: '', pairing: false};
        this._code = null;

        this.menu.setHeader(PHONE_ICON, 'iPhone Notifications');

        this._pairItem = this.menu.addAction('Pair new iPhone…', () => this._togglePairing());
        this._hintItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._hintItem.label.clutterText.lineWrap = true;
        this._hintItem.label.add_style_class_name('iphone-notify-hint');
        this.menu.addMenuItem(this._hintItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem('Recent'));
        this._recentSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._recentSection);

        this.connect('clicked', () => this._setServiceEnabled(!this._state.running));
        this._sync();
    }

    setState(patch) {
        Object.assign(this._state, patch);
        if (!this._state.pairing)
            this._code = null;
        this._sync();
    }

    setPairingCode(code) {
        this._code = String(code).padStart(6, '0');
        this._sync();
    }

    setRecent(items) {
        this._recentSection.removeAll();
        if (!items.length) {
            this._recentSection.addMenuItem(
                new PopupMenu.PopupMenuItem('No notifications yet', {reactive: false}));
            return;
        }
        for (const [app, title, body, ts] of items.slice(0, RECENT_SHOWN)) {
            const item = new PopupMenu.PopupBaseMenuItem({reactive: false});
            const box = new St.BoxLayout({orientation: Clutter.Orientation.VERTICAL, x_expand: true,
                style_class: 'iphone-notify-recent'});
            const head = new St.Label({text: `${app} · ${fmtTime(ts)}`,
                style_class: 'iphone-notify-recent-app'});
            const text = new St.Label({text: [title, body].filter(Boolean).join(' — ') || app,
                style_class: 'iphone-notify-recent-text'});
            text.clutterText.ellipsize = Pango.EllipsizeMode.END;
            box.add_child(head);
            box.add_child(text);
            item.add_child(box);
            this._recentSection.addMenuItem(item);
        }
    }

    _sync() {
        const {running, status, device, pairing} = this._state;
        this.checked = running;
        let subtitle;
        if (!running)
            subtitle = 'Off';
        else if (status === 'bluetooth-off')
            subtitle = 'Bluetooth is off';
        else if (pairing)
            subtitle = 'Pairing…';
        else if (status === 'connected')
            subtitle = device || 'Connected';
        else if (status === 'blocked')
            subtitle = 'Allow on iPhone';
        else
            subtitle = 'Not connected';
        this.subtitle = subtitle;
        this.menu.setHeader(PHONE_ICON, 'iPhone Notifications', subtitle);

        this._pairItem.visible = running && status !== 'bluetooth-off';
        this._pairItem.label.text = pairing ? 'Cancel pairing' : 'Pair new iPhone…';

        let hint = null;
        if (!running)
            hint = 'Turn on to mirror notifications from a paired iPhone.';
        else if (pairing && this._code)
            hint = `Pairing code ${this._code}\nTap Pair on the iPhone if it shows the same code, then Allow notifications.`;
        else if (pairing)
            hint = 'On the iPhone: Settings › Bluetooth, tap this computer under Other Devices.';
        else if (status === 'blocked')
            hint = `${device || 'The iPhone'} is connected but isn't sharing notifications. On the iPhone: Settings › Bluetooth › ⓘ next to this computer › turn on Share System Notifications.`;
        this._hintItem.visible = hint !== null;
        this._hintItem.label.text = hint ?? '';

        this._indicator.setConnected(running && status === 'connected');
    }

    _togglePairing() {
        this._indicator.callDaemon(this._state.pairing ? 'StopPairing' : 'StartPairing');
    }

    _setServiceEnabled(enable) {
        try {
            const proc = Gio.Subprocess.new(
                ['systemctl', '--user', enable ? 'enable' : 'disable', '--now', UNIT],
                Gio.SubprocessFlags.STDERR_PIPE);
            proc.communicate_utf8_async(null, null, (p, res) => {
                try {
                    const [, , stderr] = p.communicate_utf8_finish(res);
                    if (!p.get_successful())
                        Main.notifyError('iPhone Notifications', stderr.trim() ||
                            `Could not ${enable ? 'start' : 'stop'} ${UNIT}. Run install.sh first.`);
                } catch (e) {
                    logError(e, 'iphone-notify');
                }
            });
        } catch (e) {
            Main.notifyError('iPhone Notifications', e.message);
        }
    }
});

const IPhoneIndicator = GObject.registerClass(
class IPhoneIndicator extends SystemIndicator {
    _init() {
        super._init();
        this._icon = this._addIndicator();
        this._icon.gicon = PHONE_ICON;
        this._icon.visible = false;

        this._toggle = new IPhoneToggle(this);
        this.quickSettingsItems.push(this._toggle);

        this._proxy = null;
        this._signalIds = [];
        this._cancellable = new Gio.Cancellable();
        new DaemonProxy(Gio.DBus.session, BUS_NAME, OBJ_PATH, (proxy, error) => {
            if (error) {
                if (!error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                    logError(error, 'iphone-notify: proxy');
                return;
            }
            this._attach(proxy);
        }, this._cancellable, Gio.DBusProxyFlags.DO_NOT_AUTO_START);
    }

    _attach(proxy) {
        this._proxy = proxy;
        this._signalIds = [
            proxy.connectSignal('StateChanged', (_p, _s, [status, device, pairing]) =>
                this._toggle.setState({status, device, pairing})),
            proxy.connectSignal('PairingCode', (_p, _s, [code]) =>
                this._toggle.setPairingCode(code)),
            proxy.connectSignal('NotificationReceived', () => this._refreshRecent()),
        ];
        this._ownerId = proxy.connect('notify::g-name-owner', () => this._refresh());
        this._refresh();
    }

    async _refresh() {
        if (!this._proxy?.g_name_owner) {
            this._toggle.setState({running: false, status: 'waiting', device: '', pairing: false});
            this._toggle.setRecent([]);
            return;
        }
        try {
            const [status, device, pairing] = await this._proxy.GetStateAsync();
            this._toggle?.setState({running: true, status, device, pairing});
            await this._refreshRecent();
        } catch (e) {
            logError(e, 'iphone-notify: GetState');
        }
    }

    async _refreshRecent() {
        try {
            const [items] = await this._proxy.GetRecentAsync();
            this._toggle?.setRecent(items);
        } catch (e) {
            logError(e, 'iphone-notify: GetRecent');
        }
    }

    callDaemon(method) {
        this._proxy?.[`${method}Async`]().catch(e => logError(e, `iphone-notify: ${method}`));
    }

    setConnected(connected) {
        this._icon.visible = connected;
    }

    destroy() {
        this._cancellable.cancel();
        if (this._proxy) {
            this._signalIds.forEach(id => this._proxy.disconnectSignal(id));
            if (this._ownerId)
                this._proxy.disconnect(this._ownerId);
            this._proxy = null;
        }
        this.quickSettingsItems.forEach(item => item.destroy());
        this._toggle = null;
        super.destroy();
    }
});

export default class IPhoneNotifyExtension extends Extension {
    enable() {
        this._indicator = new IPhoneIndicator();
        Main.panel.statusArea.quickSettings.addExternalIndicator(this._indicator);
    }

    disable() {
        this._indicator?.destroy();
        this._indicator = null;
    }
}
