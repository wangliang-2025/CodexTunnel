param([Parameter(Mandatory=$true)][string]$PayloadFile)
$ErrorActionPreference = 'Stop'
$payload = Get-Content -LiteralPath $PayloadFile -Raw -Encoding UTF8 | ConvertFrom-Json
$appId = 'CodexTunnel.Desktop'
$registryPath = 'HKCU:\Software\Classes\AppUserModelId\' + $appId
New-Item -Path $registryPath -Force | Out-Null
New-ItemProperty -Path $registryPath -Name DisplayName -Value 'CodexTunnel' -PropertyType String -Force | Out-Null
New-ItemProperty -Path $registryPath -Name IconUri -Value ([string]$payload.icon) -PropertyType String -Force | Out-Null
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null
$titleText = [System.Security.SecurityElement]::Escape([string]$payload.title)
$messageText = [System.Security.SecurityElement]::Escape([string]$payload.message)
$document = New-Object Windows.Data.Xml.Dom.XmlDocument
$document.LoadXml('<toast><visual><binding template="ToastGeneric"><text>' + $titleText + '</text><text>' + $messageText + '</text></binding></visual></toast>')
$toast = [Windows.UI.Notifications.ToastNotification]::new($document)
$toast.Tag = 'connection-status'
$toast.Group = 'CodexTunnel'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
