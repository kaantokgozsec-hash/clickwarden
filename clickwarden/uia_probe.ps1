param([long]$TargetHwnd, [int]$Limit = 100, [int]$MaxNodes = 2000,
      [double]$MaxSeconds = 3, [string]$SelectedId = '', [string]$NameFilter = '')
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ProbeDpi {
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr value);
}
'@
[void][ProbeDpi]::SetThreadDpiAwarenessContext([IntPtr](-4))
$watch = [Diagnostics.Stopwatch]::StartNew()
$root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$TargetHwnd)
$walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$queue = New-Object 'System.Collections.Generic.Queue[System.Windows.Automation.AutomationElement]'
$queue.Enqueue($root)
$items = New-Object 'System.Collections.Generic.List[object]'
$scanned = 0
$skipped = 0
$stop = 'complete'
$hit = $null
$focused = $null
while ($queue.Count -gt 0) {
    if ($watch.Elapsed.TotalSeconds -ge $MaxSeconds) { $stop = 'time_limit'; break }
    if ($scanned -ge $MaxNodes) { $stop = 'node_limit'; break }
    if ($items.Count -ge $Limit) { $stop = 'result_limit'; break }
    $element = $queue.Dequeue()
    $scanned++
    try {
        $c = $element.Current
        if ($c.IsPassword) { continue } # skip the entire password subtree
        $bounds = $c.BoundingRectangle
        $id = $element.GetRuntimeId() -join ','
        if ($c.HasKeyboardFocus) {
            $focused = @{ control_id=$id; name=([string]$c.Name).Substring(0,[Math]::Min(300,([string]$c.Name).Length));
                role=$c.ControlType.ProgrammaticName }
        }
        # No ValuePattern reads: never return password or editable-field values.
        if (-not $c.IsOffscreen -and -not $c.IsPassword -and
            $bounds.Width -gt 0 -and $bounds.Height -gt 0 -and
            (-not $NameFilter -or $c.Name.IndexOf($NameFilter, [StringComparison]::OrdinalIgnoreCase) -ge 0)) {
            $box = @($bounds.Left, $bounds.Top, $bounds.Right, $bounds.Bottom)
            $controlName = $c.Name
            if ($controlName.Length -gt 300) { $controlName=$controlName.Substring(0,300) }
            $automationId = $c.AutomationId
            if ($automationId.Length -gt 200) { $automationId=$automationId.Substring(0,200) }
            $item = [ordered]@{ control_id=$id; name=$controlName; role=$c.ControlType.ProgrammaticName;
                automation_id=$automationId; enabled=$c.IsEnabled; focused=$c.HasKeyboardFocus;
                box=$box; pid=$c.ProcessId }
            $items.Add($item)
            if ($SelectedId -and $id -ceq $SelectedId) {
                # Bounded interior points, centre first.  Chromium's UIA hit test
                # can intermittently resolve a point to the page-root Document
                # even though the control's bounds are correct and the point is
                # not covered.  Every candidate is independently hit-tested and
                # must resolve to this exact control (or a descendant); the
                # click point is only returned after that validation.
                $left = [int][Math]::Floor($bounds.Left); $top = [int][Math]::Floor($bounds.Top)
                $right = [int][Math]::Ceiling($bounds.Right); $bottom = [int][Math]::Ceiling($bounds.Bottom)
                $fractions = @(@(0.5,0.5), @(0.35,0.5), @(0.65,0.5), @(0.5,0.35), @(0.5,0.65),
                               @(0.3,0.3), @(0.7,0.3), @(0.3,0.7), @(0.7,0.7))
                $seen = @{}
                $candidates = New-Object System.Collections.Generic.List[object]
                foreach ($f in $fractions) {
                    $cx = [int][Math]::Floor($bounds.Left + ($bounds.Right - $bounds.Left) * $f[0])
                    $cy = [int][Math]::Floor($bounds.Top + ($bounds.Bottom - $bounds.Top) * $f[1])
                    if ($right - $left -ge 3) {
                        if ($cx -lt $left + 1) { $cx = $left + 1 }
                        if ($cx -gt $right - 2) { $cx = $right - 2 }
                    }
                    if ($bottom - $top -ge 3) {
                        if ($cy -lt $top + 1) { $cy = $top + 1 }
                        if ($cy -gt $bottom - 2) { $cy = $bottom - 2 }
                    }
                    $key = "$cx,$cy"
                    if (-not $seen.ContainsKey($key)) {
                        $seen[$key] = $true
                        $candidates.Add(@{ x=$cx; y=$cy; fx=$f[0]; fy=$f[1] })
                    }
                }
                $hit = $null
                $firstFailure = $null
                $tried = 0
                foreach ($c in $candidates) {
                    $tried++
                    $p = New-Object System.Windows.Point([Math]::Floor($c.x), [Math]::Floor($c.y))
                    $hitElement = [System.Windows.Automation.AutomationElement]::FromPoint($p)
                    $actualHit = $null
                    if ($null -ne $hitElement) {
                        $hb = $hitElement.Current.BoundingRectangle
                        $hn = [string]$hitElement.Current.Name
                        if ($hn.Length -gt 80) { $hn = $hn.Substring(0,80) }
                        $actualHit = @{ control_id=($hitElement.GetRuntimeId() -join ',');
                            role=$hitElement.Current.ControlType.ProgrammaticName;
                            pid=$hitElement.Current.ProcessId; hwnd=$hitElement.Current.NativeWindowHandle;
                            box=@([int]$hb.Left,[int]$hb.Top,[int]$hb.Right,[int]$hb.Bottom); name=$hn }
                    }
                    $matched = $false
                    # A child label can occupy a point inside a button. Require the
                    # hit element or an ancestor to be exactly the selected control.
                    for ($depth=0; $depth -lt 32 -and $null -ne $hitElement; $depth++) {
                        if (($hitElement.GetRuntimeId() -join ',') -ceq $id) { $matched=$true; break }
                        $hitElement = [System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($hitElement)
                    }
                    $record = @{ control_id=$id; matches=$matched; actual=$actualHit;
                        point=@([Math]::Floor($c.x),[Math]::Floor($c.y));
                        fraction=@($c.fx,$c.fy) }
                    if ($matched) { $hit = $record; break }
                    if ($null -eq $firstFailure) { $firstFailure = $record }
                }
                if ($null -eq $hit) { $hit = $firstFailure }
                $hit['candidates_tried'] = $tried
                $hit['center_ok'] = ($hit['fraction'][0] -eq 0.5 -and $hit['fraction'][1] -eq 0.5)
            }
        }
        $child = $walker.GetFirstChild($element)
        while ($null -ne $child) {
            if ($queue.Count+$scanned -ge $MaxNodes) { $stop='node_limit'; break }
            if ($watch.Elapsed.TotalSeconds -ge $MaxSeconds) { $stop='time_limit'; break }
            $queue.Enqueue($child)
            $child = $walker.GetNextSibling($child)
        }
    } catch { $skipped++ }
}
@{ controls=@($items.ToArray()); scanned=$scanned; skipped=$skipped;
   complete=($stop -eq 'complete' -and $skipped -eq 0); stop_reason=$stop;
   hit=$hit; focused_control=$focused; elapsed_ms=$watch.ElapsedMilliseconds } | ConvertTo-Json -Depth 6 -Compress
