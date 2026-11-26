# Asks the owner for the contact address the SEC requires and saves it as the
# EDGAR_IDENTITY variable of this Windows account. Nothing is sent anywhere by
# this script, and the address is not written to any file of the project.
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Market Signals - identificare pentru SEC'
$form.Size = New-Object System.Drawing.Size(520, 230)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$label = New-Object System.Windows.Forms.Label
$label.Location = New-Object System.Drawing.Point(16, 14)
$label.Size = New-Object System.Drawing.Size(480, 62)
$label.Text = "SEC cere o adresa de contact pentru cine ii descarca datele publice.`r`nScrie adresa ta de e-mail si apasa Salveaza. Adresa ajunge doar la sec.gov."
$form.Controls.Add($label)

$box = New-Object System.Windows.Forms.TextBox
$box.Location = New-Object System.Drawing.Point(16, 84)
$box.Size = New-Object System.Drawing.Size(472, 24)
$form.Controls.Add($box)

$ok = New-Object System.Windows.Forms.Button
$ok.Location = New-Object System.Drawing.Point(292, 130)
$ok.Size = New-Object System.Drawing.Size(96, 32)
$ok.Text = 'Salveaza'
$ok.DialogResult = [System.Windows.Forms.DialogResult]::OK
$form.AcceptButton = $ok
$form.Controls.Add($ok)

$cancel = New-Object System.Windows.Forms.Button
$cancel.Location = New-Object System.Drawing.Point(392, 130)
$cancel.Size = New-Object System.Drawing.Size(96, 32)
$cancel.Text = 'Renunta'
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$form.CancelButton = $cancel
$form.Controls.Add($cancel)

$form.Add_Shown({ $form.Activate(); $box.Focus() })

while ($true) {
    $result = $form.ShowDialog()
    if ($result -ne [System.Windows.Forms.DialogResult]::OK) { exit 1 }
    $address = $box.Text.Trim()
    if ($address -match '^[^@\s]+@[^@\s]+\.[^@\s]+$') { break }
    [void][System.Windows.Forms.MessageBox]::Show('Adresa nu pare valida. Incearca din nou.', 'Market Signals', 'OK', 'Warning')
}

[Environment]::SetEnvironmentVariable('EDGAR_IDENTITY', "Market Signals research $address", 'User')
[void][System.Windows.Forms.MessageBox]::Show('Salvat. Poti inchide fereastra si ii poti spune lui Claude ca e gata.', 'Market Signals', 'OK', 'Information')
exit 0
