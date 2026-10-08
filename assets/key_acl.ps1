param([Parameter(Mandatory=$true)][string]$KeyPath,[Parameter(Mandatory=$true)][string]$OwnerSid)
$ErrorActionPreference = 'Stop'
$acl = Get-Acl -LiteralPath $KeyPath
$allowed = @($OwnerSid, 'S-1-5-18', 'S-1-5-32-544')
$owner = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
if ($owner -notin $allowed) { throw 'Private key owner is outside the trusted scope.' }
$rules = $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])
foreach ($rule in $rules) {
    if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Allow -and $rule.IdentityReference.Value -notin $allowed) {
        throw 'Private key ACL grants another identity access.'
    }
}
$stream = [System.IO.File]::OpenRead($KeyPath)
$stream.Close()
