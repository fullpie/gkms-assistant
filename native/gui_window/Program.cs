// A presentation-only Windows window. All game/control work stays in the Python owner.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Runtime.Serialization;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[DataContract]
internal sealed class Launch {
    [DataMember] public string schema;
    [DataMember] public string url;
    [DataMember] public string user_data;
    [DataMember] public string report;
    [DataMember] public string title;
    [DataMember] public int owner_pid;
    [DataMember] public bool capture;
    [DataMember] public bool probe_close;
}

internal static class DpiSupport {
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetProcessDpiAwarenessContext(IntPtr context);
    [DllImport("shcore.dll")]
    private static extern int SetProcessDpiAwareness(int awareness);
    [DllImport("user32.dll")]
    private static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")]
    private static extern IntPtr GetThreadDpiAwarenessContext();
    [DllImport("user32.dll")]
    private static extern bool AreDpiAwarenessContextsEqual(IntPtr first, IntPtr second);
    [DllImport("user32.dll")]
    private static extern bool IsProcessDPIAware();

    internal static string ContextName() {
        try {
            IntPtr context = GetThreadDpiAwarenessContext();
            if (AreDpiAwarenessContextsEqual(context, new IntPtr(-4))) return "PerMonitorV2";
            if (AreDpiAwarenessContextsEqual(context, new IntPtr(-3))) return "PerMonitor";
            if (AreDpiAwarenessContextsEqual(context, new IntPtr(-2))) return "SystemAware";
            return "Unaware";
        } catch (EntryPointNotFoundException) { return IsProcessDPIAware() ? "SystemAware" : "Unaware"; }
          catch (DllNotFoundException) { return "Unavailable"; }
    }

    internal static void EnableFallback() {
        // .NET 4.7+ app.config normally establishes PMv2 in EnableVisualStyles.
        // Older Windows builds may not support that opt-in. Set a supported
        // process mode only before any HWND exists; never downgrade a set mode.
        if (ContextName() != "Unaware") return;
        try {
            if (SetProcessDpiAwarenessContext(new IntPtr(-4)) || Marshal.GetLastWin32Error() == 5) return;
        } catch (EntryPointNotFoundException) { }
          catch (DllNotFoundException) { }
        try {
            int result = SetProcessDpiAwareness(2);
            if (result == 0 || result == unchecked((int)0x80070005)) return;
        } catch (EntryPointNotFoundException) { }
          catch (DllNotFoundException) { }
        try { SetProcessDPIAware(); }
        catch (EntryPointNotFoundException) { }
        catch (DllNotFoundException) { }
    }
}

internal sealed class AssistantWindow : Form {
    private static TextReader commandInput;
    private readonly Launch launch;
    private readonly Uri endpoint;
    private readonly string token;
    private readonly WebView2 view = new WebView2();
    private readonly HttpClient http;
    private readonly Process owner;
    private readonly Timer timer = new Timer();
    private bool allowedClose;
    private bool closeRequested;
    private string closeRequestId;
    private int ticks;
    private bool snapshotSaved;

    internal AssistantWindow(Launch input) {
        launch = input;
        endpoint = new Uri(input.url);
        if (input.schema != "gkms.native-window-launch.v1" || endpoint.Scheme != "http" ||
            endpoint.Host != "127.0.0.1" || endpoint.Port <= 0 || endpoint.AbsolutePath != "/" ||
            endpoint.Query != "" || endpoint.UserInfo != "" || !endpoint.Fragment.StartsWith("#token="))
            throw new InvalidOperationException("Invalid local application connection.");
        token = endpoint.Fragment.Substring(7);
        if (token.Length < 24 || token.Length > 128 || !System.Text.RegularExpressions.Regex.IsMatch(token, "^[A-Za-z0-9_-]+$"))
            throw new InvalidOperationException("Invalid local application credential.");
        if (!Path.IsPathRooted(input.user_data) || !Path.IsPathRooted(input.report) ||
            Path.GetFullPath(Path.GetDirectoryName(input.report)) != Path.GetFullPath(input.user_data))
            throw new InvalidOperationException("Invalid application state directory.");
        Directory.CreateDirectory(input.user_data);
        owner = Process.GetProcessById(input.owner_pid);
        // Pin this process object; a reused numerical PID is not the GUI owner.
        var ownerHandle = owner.Handle;
        if (owner.HasExited) throw new InvalidOperationException("Application owner already ended.");
        http = new HttpClient(new HttpClientHandler { UseProxy = false });
        http.Timeout = TimeSpan.FromSeconds(8);
        http.DefaultRequestHeaders.Add("X-GKMS-Token", token);
        http.DefaultRequestHeaders.Add("Origin", endpoint.GetLeftPart(UriPartial.Authority));
        SuspendLayout();
        AutoScaleDimensions = new SizeF(96F, 96F);
        AutoScaleMode = AutoScaleMode.Dpi;
        Text = string.IsNullOrEmpty(input.title) ? "GKMS Assistant" : input.title;
        ClientSize = new Size(1280, 850);
        MinimumSize = new Size(900, 640);
        StartPosition = FormStartPosition.CenterScreen;
        Icon = SystemIcons.Application;
        view.Dock = DockStyle.Fill;
        Controls.Add(view);
        ResumeLayout(false);
        Shown += async delegate { await InitializeView(); };
        FormClosing += OnClosing;
        FormClosed += delegate { timer.Stop(); view.Dispose(); http.Dispose(); owner.Dispose(); };
        timer.Interval = 500;
        timer.Tick += async delegate { await CheckOwner(); };
        timer.Start();
        Task.Run(() => {
            string command;
            while ((command = commandInput.ReadLine()) != null) {
                if (command == "owner-stopped") break;
            }
            try { BeginInvoke(new Action(() => { allowedClose = true; Close(); })); }
            catch (InvalidOperationException) { }
        });
    }

    private async Task InitializeView() {
        try {
            var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(launch.user_data, "profile"));
            await view.EnsureCoreWebView2Async(environment);
            view.CoreWebView2.Settings.AreDevToolsEnabled = false;
            view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
            view.CoreWebView2.Settings.IsStatusBarEnabled = false;
            view.CoreWebView2.NavigationStarting += (sender, args) => {
                Uri target;
                if (!Uri.TryCreate(args.Uri, UriKind.Absolute, out target) ||
                    target.GetLeftPart(UriPartial.Authority) != endpoint.GetLeftPart(UriPartial.Authority)) args.Cancel = true;
            };
            view.CoreWebView2.NewWindowRequested += (sender, args) => {
                args.Handled = true;
                Uri target;
                if (args.IsUserInitiated && Uri.TryCreate(args.Uri, UriKind.Absolute, out target) &&
                    target.Scheme == "https" && target.UserInfo == "" && !target.Fragment.Contains(token))
                    Process.Start(new ProcessStartInfo(target.AbsoluteUri) { UseShellExecute = true });
            };
            view.CoreWebView2.PermissionRequested += (sender, args) => args.State = CoreWebView2PermissionState.Deny;
            view.CoreWebView2.DownloadStarting += (sender, args) => args.Cancel = true; // Downloads use the verified owner updater.
            view.CoreWebView2.NavigationCompleted += async (sender, args) => {
                if (args.IsSuccess) {
                    WriteReport("ready", null);
                    try { await CaptureOwnView(); } catch (Exception) { }
                } else WriteReport("navigation-failed", args.WebErrorStatus.ToString());
            };
            view.CoreWebView2.Navigate(launch.url);
        } catch (Exception error) {
            WriteReport("failed", error.GetType().Name);
            MessageBox.Show(this, "無法開啟應用視窗。請確認已安裝 Microsoft Edge WebView2 Runtime。\n" +
                "The application window requires Microsoft Edge WebView2 Runtime.", "GKMS Assistant", MessageBoxButtons.OK, MessageBoxIcon.Error);
            Close();
        }
    }

    private async Task CaptureOwnView() {
        if (!launch.capture || snapshotSaved || view.CoreWebView2 == null) return;
        string ready = await view.CoreWebView2.ExecuteScriptAsync("document.querySelector('.side-version') !== null");
        if (ready != "true") return;
        using (var stream = File.Create(Path.ChangeExtension(launch.report, ".png")))
            await view.CoreWebView2.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
        snapshotSaved = true;
        if (launch.probe_close) Close(); // Owner-provided isolated lifecycle test; never enabled by normal GUI.
    }

    private async void OnClosing(object sender, FormClosingEventArgs args) {
        if (allowedClose || owner.HasExited) return;
        args.Cancel = true;
        if (closeRequested) return;
        closeRequested = true;
        if (closeRequestId == null) closeRequestId = Guid.NewGuid().ToString("N");
        Text = "GKMS Assistant · 正在停止並退出";
        try {
            string body = "{\"request_id\":\"" + closeRequestId + "\",\"callback\":\"shutdown\",\"values\":{}}";
            using (var response = await http.PostAsync(new Uri(endpoint, "/api/command"),
                new StringContent(body, Encoding.UTF8, "application/json"))) {
                if (!response.IsSuccessStatusCode) {
                    closeRequested = false;
                    Text = launch.title;
                    MessageBox.Show(this, "助手尚未確認停止。請等待目前操作，然後再關閉視窗。", "GKMS Assistant");
                }
            }
        } catch (Exception) {
            // Unknown response: keep the same close ID; never kill the owner or game.
            closeRequested = false;
            Text = "GKMS Assistant · 等待停止確認";
        }
    }

    private async Task CheckOwner() {
        if (owner.HasExited) { allowedClose = true; Close(); return; }
        if (++ticks % 4 == 0 && !snapshotSaved && view.CoreWebView2 != null) {
            try { await CaptureOwnView(); } catch (Exception) { }
        }
    }

    private void WriteReport(string state, string error) {
        var data = new System.Collections.Generic.Dictionary<string, object> {
            {"schema", "gkms.native-window-status.v1"}, {"state", state}, {"pid", Process.GetCurrentProcess().Id},
            {"owner_pid", launch.owner_pid}, {"window_handle", Handle.ToInt64()}, {"presentation", "native-webview2"},
            {"dpi_awareness", DpiSupport.ContextName()}, {"window_dpi", DeviceDpi},
            {"client_width", ClientSize.Width}, {"client_height", ClientSize.Height},
            {"external_browser_opened", false}, {"game_io", false}, {"error", error}
        };
        string temp = launch.report + ".tmp";
        var serializer = new DataContractJsonSerializer(data.GetType(), new DataContractJsonSerializerSettings { UseSimpleDictionaryFormat = true });
        using (var stream = File.Create(temp)) serializer.WriteObject(stream, data);
        if (File.Exists(launch.report)) File.Replace(temp, launch.report, null); else File.Move(temp, launch.report);
    }

    [STAThread]
    private static int Main(string[] args) {
        try {
            Application.EnableVisualStyles();
            DpiSupport.EnableFallback();
            Application.SetCompatibleTextRenderingDefault(false);
            if (args.Length == 1 && args[0] == "--dpi-probe") {
                // No Form, WebView, owner connection, credential or native I/O.
                Console.WriteLine("{\"schema\":\"gkms.native-window-dpi-probe.v1\",\"dpi_awareness\":\"" +
                    DpiSupport.ContextName() + "\",\"window_created\":false,\"game_io\":false}");
                return 0;
            }
            if (args.Length != 0) return 2;
            commandInput = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true));
            string input = commandInput.ReadLine();
            if (input == null || input.Length > 16384) return 2;
            Launch launch;
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(input)))
                launch = (Launch)new DataContractJsonSerializer(typeof(Launch)).ReadObject(stream);
            Application.Run(new AssistantWindow(launch));
            return 0;
        } catch (Exception error) {
            // Type/stack only: never print the private launch payload or token.
            Console.Error.WriteLine(error.GetType().FullName + "\n" + error.StackTrace);
            return 2;
        }
    }
}
