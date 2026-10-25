# Reads the Error List of every running Visual Studio instance through the
# documented EnvDTE automation (COM) API. Read-only: it never builds,
# edits, or changes anything in Visual Studio.
#
# Protocol: for every line read on stdin ("poll", or "poll+unsaved" to also
# return the text of documents with unsaved changes) one compressed JSON
# document is written to stdout. Closing stdin (or sending "exit") ends it.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;

public static class SelRot {
    [DllImport("ole32.dll")] static extern int GetRunningObjectTable(int reserved, out IRunningObjectTable rot);
    [DllImport("ole32.dll")] static extern int CreateBindCtx(int reserved, out IBindCtx ctx);

    // Every Visual Studio instance registers "!VisualStudio.DTE.<ver>:<pid>"
    // in the Running Object Table; Marshal.GetActiveObject would only
    // return the first one.
    public static List<KeyValuePair<string, object>> DteInstances() {
        var found = new List<KeyValuePair<string, object>>();
        IRunningObjectTable rot;
        if (GetRunningObjectTable(0, out rot) != 0) return found;
        IEnumMoniker monikers;
        rot.EnumRunning(out monikers);
        var one = new IMoniker[1];
        while (monikers.Next(1, one, IntPtr.Zero) == 0) {
            try {
                IBindCtx ctx;
                CreateBindCtx(0, out ctx);
                string name;
                one[0].GetDisplayName(ctx, null, out name);
                if (name != null && name.StartsWith("!VisualStudio.DTE.")) {
                    object dte;
                    rot.GetObject(one[0], out dte);
                    found.Add(new KeyValuePair<string, object>(name, dte));
                }
            } catch { }
        }
        return found;
    }
}
"@

$maxItems = 2000
$maxUnsavedDocs = 20
$maxUnsavedChars = 2000000
while ($true) {
    $request = [Console]::In.ReadLine()
    if ($request -eq $null -or $request -eq 'exit') { break }
    # "poll" = Error List only; "poll+unsaved" also returns unsaved text.
    $includeUnsaved = $request -eq 'poll+unsaved'

    $instances = New-Object System.Collections.Generic.List[object]
    try {
        foreach ($entry in [SelRot]::DteInstances()) {
            $dte = $entry.Value
            try {
                $errorItems = $dte.ToolWindows.ErrorList.ErrorItems
                $items = New-Object System.Collections.Generic.List[object]
                $count = [Math]::Min([int]$errorItems.Count, $maxItems)
                for ($i = 1; $i -le $count; $i++) {
                    $e = $errorItems.Item($i)
                    $items.Add([pscustomobject]@{
                        level = [int]$e.ErrorLevel
                        description = [string]$e.Description
                        file = [string]$e.FileName
                        line = [int]$e.Line
                        column = [int]$e.Column
                        project = [string]$e.Project
                    })
                }
                $activeDoc = $null
                try { if ($dte.ActiveDocument) { $activeDoc = [string]$dte.ActiveDocument.FullName } } catch { }
                # Text of open documents with unsaved changes, so errors can
                # be found before saving. Read-only: only GetText is used.
                $unsaved = New-Object System.Collections.Generic.List[object]
                if ($includeUnsaved) {
                    foreach ($doc in $dte.Documents) {
                        try {
                            if ($doc.Saved -or $unsaved.Count -ge $maxUnsavedDocs) { continue }
                            $textDoc = $doc.Object("TextDocument")
                            if ($textDoc -eq $null) { continue }
                            $text = $textDoc.StartPoint.CreateEditPoint().GetText($textDoc.EndPoint)
                            if ($text.Length -gt $maxUnsavedChars) { continue }
                            $unsaved.Add([pscustomobject]@{ path = [string]$doc.FullName; text = [string]$text })
                        } catch { }
                    }
                }
                $instances.Add([pscustomobject]@{
                    moniker = $entry.Key
                    ok = $true
                    solution = [string]$dte.Solution.FullName
                    activeDocument = $activeDoc
                    items = $items.ToArray()
                    unsaved = $unsaved.ToArray()
                })
            } catch {
                # Typically RPC_E_CALL_REJECTED while VS is busy (building,
                # a modal dialog is open); the caller treats it as transient.
                $instances.Add([pscustomobject]@{ moniker = $entry.Key; ok = $false; error = $_.Exception.Message })
            }
        }
        $result = [pscustomobject]@{ ok = $true; instances = $instances.ToArray() }
    } catch {
        $result = [pscustomobject]@{ ok = $false; error = $_.Exception.Message; instances = @() }
    }
    [Console]::Out.WriteLine(($result | ConvertTo-Json -Depth 6 -Compress))
    [Console]::Out.Flush()
}
