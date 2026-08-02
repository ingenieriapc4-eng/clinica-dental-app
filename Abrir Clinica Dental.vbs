' Abre la Clínica Dental. La primera vez muestra la ventana (para ver si falta
' instalar algo); las siguientes veces arranca sin mostrar ninguna ventana negra.
' Este es el archivo que debes usar para tu acceso directo del escritorio.
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
carpeta = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = carpeta

If fso.FileExists(carpeta & "\.deps_ok") Then
    shell.Run """" & carpeta & "\iniciar_silencioso.bat""", 0, False
Else
    shell.Run """" & carpeta & "\iniciar.bat""", 1, True
End If
