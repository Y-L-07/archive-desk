using namespace System.Windows.Automation
param([switch]$SourcePreflight, [ValidateSet('V4','V5')][string]$Version='V4')

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
# Invoke on a worker so a Qt slot opening a modal dialog cannot block its driver.
Add-Type -ReferencedAssemblies UIAutomationClient,UIAutomationTypes -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Windows.Automation;
public static class ArchiveSmokeAction {
    [StructLayout(LayoutKind.Sequential)] struct Point { public int X,Y; }
    [DllImport("user32.dll")] static extern bool ScreenToClient(IntPtr window,ref Point p);
    [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr window,uint msg,IntPtr wp,IntPtr lp);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window,out uint pid);
    public static void ToggleTreeRow(IntPtr window,int pid,int x,int y) {
        uint owner;
        if (GetWindowThreadProcessId(window,out owner)==0 || owner!=pid)
            throw new InvalidOperationException("Unexpected window owner");
        var p = new Point { X=x, Y=y };
        if (!ScreenToClient(window,ref p)) throw new InvalidOperationException("Window unavailable");
        var point = (IntPtr)((p.Y << 16) | (p.X & 65535));
        SendMessage(window,0x201,(IntPtr)1,point);
        SendMessage(window,0x202,IntPtr.Zero,point);
        SendMessage(window,0x100,(IntPtr)32,(IntPtr)0x390001);
        SendMessage(window,0x101,(IntPtr)32,(IntPtr)unchecked((int)0xc0390001));
    }
    public static Task Press(AutomationElement node) {
        return Task.Run(() => ((InvokePattern)node.GetCurrentPattern(InvokePattern.Pattern)).Invoke());
    }
}
"@
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runTemp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
$scratch = Join-Path $runTemp ('archive-exe-' + [Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($scratch)
$app = $null
$watch = [Diagnostics.Stopwatch]::StartNew()

function Assert-True($condition, [string]$message) {
    if (-not $condition) { throw $message }
}
function Wait-State([string]$label, [scriptblock]$probe) {
    $end = $watch.Elapsed.TotalSeconds + 40
    do {
        if ($null -ne $app -and $app.HasExited) { throw "应用进程意外退出：$label" }
        try { $value = & $probe; if ($value) { return $value } }
        catch [System.Windows.Automation.ElementNotAvailableException] { }
        Start-Sleep -Milliseconds 120
    } while ($watch.Elapsed.TotalSeconds -lt $end)
    throw "等待超时：$label"
}
function App-Windows {
    $condition = [PropertyCondition]::new([AutomationElement]::ProcessIdProperty,[int]$app.Id)
    [AutomationElement]::RootElement.FindAll([TreeScope]::Children,$condition)
}
function By-Name([string]$name, $type) {
    $c = [PropertyCondition]::new([AutomationElement]::NameProperty,$name)
    if ($null -ne $type) {
        $c = [AndCondition]::new($c,[PropertyCondition]::new([AutomationElement]::ControlTypeProperty,$type))
    }
    foreach ($window in (App-Windows)) {
        foreach ($node in $window.FindAll([TreeScope]::Descendants,$c)) {
            if ($node.Current.ProcessId -eq $app.Id -and $node.Current.IsEnabled -and -not $node.Current.IsOffscreen) {
                return $node
            }
        }
    }
}
function Press([string]$name) {
    $node = Wait-State $name {
        $button = By-Name $name ([ControlType]::Button)
        if ($null -ne $button) { return $button }
        By-Name $name ([ControlType]::CheckBox)
    }
    [void][ArchiveSmokeAction]::Press($node)
}
function Find-Text([string]$part) {
    foreach ($window in (App-Windows)) {
        $nodes = $window.FindAll([TreeScope]::Descendants,[Condition]::TrueCondition)
        foreach ($node in $nodes) {
            if ($node.Current.ProcessId -eq $app.Id -and -not $node.Current.IsOffscreen -and
                    $node.Current.Name -and $node.Current.Name.Contains($part)) { return $node.Current.Name }
        }
    }
}
function Visible-Text([string]$part) { Wait-State $part { Find-Text $part } }
function Start-Isolated([string]$name) {
    $state = Join-Path $scratch $name
    $tmp = Join-Path $state 'temp'
    [void][IO.Directory]::CreateDirectory($tmp)
    $start = [Diagnostics.ProcessStartInfo]::new()
    if ($SourcePreflight) {
        $cfg = Get-Content -LiteralPath (Join-Path $project '.venv\pyvenv.cfg')
        $basePython = (($cfg | Where-Object {$_ -match '^home\s*='}) -split '=',2)[1].Trim()
        $start.FileName = Join-Path $basePython 'pythonw.exe'
        $start.Arguments = '"' + (Join-Path $project 'main.py') + '"'
        $start.EnvironmentVariables['PYTHONPATH'] = Join-Path $project '.venv\Lib\site-packages'
    } else {
        $start.FileName = (Resolve-Path -LiteralPath (Join-Path $project "dist\$Version\归档台\归档台.exe")).Path
    }
    $start.WorkingDirectory = $state
    $start.UseShellExecute = $false
    $start.EnvironmentVariables['TEMP'] = $tmp
    $start.EnvironmentVariables['TMP'] = $tmp
    $start.EnvironmentVariables['ARCHIVE_DESK_DATA_DIR'] = Join-Path $state 'appdata'
    $start.EnvironmentVariables['QT_QPA_PLATFORM'] = 'windows'
    $start.EnvironmentVariables['QT_ACCESSIBILITY'] = '1'
    $script:app = [Diagnostics.Process]::Start($start)
    Press '备份与找回'
}
function Stop-Isolated {
    if ($null -ne $app -and -not $app.HasExited) {
        [void]$app.CloseMainWindow()
        if (-not $app.WaitForExit(4500)) {
            # This is precisely the process handle returned by Start(), never
            # another open instance of the user's desktop application.
            $app.Kill()
            $app.WaitForExit()
        }
    }
    if ($null -ne $app) { $app.Dispose(); $script:app = $null }
}
function Choose-Folder([string]$button,[string]$title,[string]$path) {
    Assert-True ($path.StartsWith($scratch+'\',[StringComparison]::OrdinalIgnoreCase)) '文件夹对话框目标必须在测试目录内'
    Press $button
    $dialog = Wait-State $title {
        foreach ($w in (App-Windows)) { if ($w.Current.Name -eq $title) { return $w } }
        By-Name $title ([ControlType]::Window)
    }
    # The native Windows folder picker owns a file-name combo (ID 1148).
    # Only inspect that dialog's controls, and use ValuePattern rather than
    # global keyboard/mouse input to set the exact, validated directory.
    $edit = Wait-State '文件夹路径输入框' {
        foreach ($node in $dialog.FindAll([TreeScope]::Descendants,
                [PropertyCondition]::new([AutomationElement]::ControlTypeProperty,[ControlType]::Edit))) {
            if ($node.Current.IsEnabled -and -not $node.Current.IsOffscreen -and
                   ($node.Current.AutomationId -eq '1148' -or $node.Current.Name -match '文件夹|Folder name')) {
                return $node
            }
        }
    }
    $edit.GetCurrentPattern([ValuePattern]::Pattern).SetValue($path)
    $ok = Wait-State '选择文件夹确认按钮' {
        $condition = [AndCondition]::new(
            [PropertyCondition]::new([AutomationElement]::AutomationIdProperty,'1'),
            [PropertyCondition]::new([AutomationElement]::ControlTypeProperty,[ControlType]::Button))
        $node = $dialog.FindFirst([TreeScope]::Descendants,$condition)
        if ($null -ne $node -and $node.Current.IsEnabled) { $node }
    }
    [void][ArchiveSmokeAction]::Press($ok)
}
function Check-Payload([string]$source,[string]$repo) {
    $versions = @(Get-ChildItem -LiteralPath (Join-Path $repo 'versions') -File -Filter '*.json')
    Assert-True ($versions.Count -eq 1) '预期仅生成一个版本'
    $json = $versions[0]
    $shaPath = Join-Path $json.DirectoryName ($json.BaseName + '.sha256')
    Assert-True ((Get-Content -LiteralPath $shaPath -Raw).Trim() -eq (Get-FileHash -LiteralPath $json.FullName).Hash) '清单摘要不匹配'
    $manifest = Get-Content -LiteralPath $json.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-True ($manifest.status -eq 'complete' -and $manifest.files.Count -eq 4) '版本未完整保存四个样例文件'
    $allowed = @('交接/项目说明.txt','交接/清单.txt','参考/清单副本.txt','样例.bin')
    foreach ($entry in $manifest.files) {
        Assert-True ($allowed -ccontains $entry.path -and $entry.sha256 -cmatch '^[a-f0-9]{64}$') '发现非预期路径或摘要'
        $original = Join-Path $source ($entry.path -replace '/','\')
        $blob = Join-Path (Join-Path (Join-Path $repo 'objects') $entry.sha256.Substring(0,2)) $entry.sha256
        Assert-True ((Get-FileHash -LiteralPath $blob).Hash -eq (Get-FileHash -LiteralPath $original).Hash -and
                (Get-FileHash -LiteralPath $blob).Hash -eq $entry.sha256) '对象、清单及源文件摘要不一致'
    }
    Assert-True (@($manifest.files.sha256 | Sort-Object -Unique).Count -eq 3) '相同内容未复用'
    Assert-True (@(Get-ChildItem -LiteralPath (Join-Path $repo 'objects') -File -Recurse -Force).Count -eq 3) '仓库中实际对象数不符'
    return $manifest
}

try {
    Start-Isolated 'first'
    Press '创建备份演示'
    $demoParent = Join-Path $scratch 'first\temp'
    $demo = Wait-State '独立备份演示' {
        $dirs = @(Get-ChildItem -LiteralPath $demoParent -Directory -Filter 'ArchiveDesk-backup-demo-*')
        if ($dirs.Count -eq 1 -and (Test-Path -LiteralPath (Join-Path $dirs[0].FullName '项目资料\样例.bin'))) { $dirs[0].FullName }
    }
    $source = Join-Path $demo '项目资料'
    $repo = Join-Path $demo '备份仓库'
    Assert-True ([IO.Path]::GetDirectoryName($demo) -eq $demoParent) '演示不在本进程 TEMP 内'
    [void](Visible-Text $source)
    [void](Visible-Text $repo)
    Assert-True (@(Get-ChildItem -LiteralPath $repo -Force).Count -eq 0) '预检前仓库应为空'
    Press '预检备份范围'
    [void](Visible-Text '预估 4 个文件')
    Assert-True (@(Get-ChildItem -LiteralPath $repo -Force).Count -eq 0) '只读预检写入了仓库'
    Press '开始备份'
    $confirmation = Visible-Text '将创建新的备份版本'
    Assert-True ($confirmation.Contains($source) -and $confirmation.Contains($repo)) '确认框中的源或仓库不属于临时演示'
    Press '确认备份'
    $browse = Wait-State '可浏览的已提交仓库' { By-Name '浏览此仓库' ([ControlType]::Button) }
    $manifest = Check-Payload $source $repo
    [void][ArchiveSmokeAction]::Press($browse)
    [void](Visible-Text $manifest.id)
    Press '验证此仓库'
    [void](Visible-Text '完整性问题 0 条')
    Write-Host '可见窗口备份及校验通过：4 个文件、3 个 SHA-256 内容对象，预检未写入仓库。'
    Stop-Isolated

    $restored = Join-Path $scratch 'recovered'
    [void][IO.Directory]::CreateDirectory($restored)
    Start-Isolated 'fresh'
    Choose-Folder '导入已有仓库…' '导入已有备份仓库' $repo
    [void](Visible-Text $manifest.id)
    $item = Wait-State '整个版本的恢复选择' { By-Name '整个版本' ([ControlType]::TreeItem) }
    # Qt exposes a TogglePattern on tree cells, but some versions do not
    # relay it to the item model. Select this row and press Space through the
    # specific test window's message queue without sending desktop-wide input.
    $rect = $item.Current.BoundingRectangle
    [ArchiveSmokeAction]::ToggleTreeRow($app.MainWindowHandle,$app.Id,
        [int]($rect.X+$rect.Width/2),[int]($rect.Y+$rect.Height/2))
    [void](Visible-Text '已选择 整个版本')
    Choose-Folder '选择目录…' '选择单独的恢复目录' $restored
    Press '预览恢复计划'
    [void](Visible-Text '恢复文件 4')
    Assert-True (@(Get-ChildItem -LiteralPath $restored -Force).Count -eq 0) '恢复预览写入了目标'
    Press '执行恢复'
    $confirmation = Visible-Text '将按预览写入并校验文件'
    Assert-True ($confirmation.Contains($restored) -and $confirmation.Contains($repo)) '恢复确认路径不属于本次测试'
    Press '确认恢复'
    [void](Visible-Text '已恢复文件 4')
    Assert-True (@(Get-ChildItem -LiteralPath $restored -File -Recurse -Force).Count -eq $manifest.files.Count) '恢复后的文件总数不符'
    foreach ($entry in $manifest.files) {
        $actual = Join-Path $restored $entry.path
        Assert-True ((Get-FileHash -LiteralPath $actual).Hash -eq $entry.sha256) '恢复内容不一致'
    }
    Assert-True ([IO.Directory]::Exists((Join-Path $restored '空目录'))) '未恢复空目录'
    Write-Host '第二个空白配置进程导入仓库并恢复整版：路径、空目录和全部文件 SHA-256 一致。'
}
finally {
    Stop-Isolated
    $actualScratch = [IO.Path]::GetFullPath($scratch)
    Assert-True (([IO.Path]::GetDirectoryName($actualScratch) -eq $runTemp) -and
                 ([IO.Path]::GetFileName($actualScratch) -match '^archive-exe-[a-f0-9]{32}$')) '清理范围错误'
    [IO.Directory]::Delete($actualScratch,$true)
}
