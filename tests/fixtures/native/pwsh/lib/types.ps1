class Helper {
    static [void] Do() { Write-Output 1 }
    [void] Unused() { Write-Output 2 }
}
class Lonely { }
class Widget { Widget() { Write-Output 3 } }
function Plainfn { Write-Output 4 }
