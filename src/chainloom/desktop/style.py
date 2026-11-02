STYLE = """
QMainWindow, QWidget#content { background: #FDFEFC; }
QWidget#settingsPage, QWidget#transferColumns, QWidget#transferForm { background: #FDFEFC; }
QWidget { font-family: 'PingFang SC'; font-size: 13px; color: #263C32; }
QFrame#sidebar { background: #F3F6F0; border-right: 1px solid #E5EAE1; }
QFrame#summary, QFrame#connectionCard { background: #F4F7F0; border: 1px solid #E3E9DE; border-radius: 14px; }
QLabel { background: transparent; border: none; }
QLabel#heading { font-size: 28px; font-weight: 650; color: #1C2D2A; }
QLabel#brand { font-size: 21px; font-weight: 650; }
QLabel#muted { color: #7B897F; font-size: 12px; }
QLabel#fieldLabel { color: #5B7061; font-size: 12px; font-weight: 550; }
QFrame#settingsSection { background: white; border: 1px solid #E3E9DE; border-radius: 12px; }
QLabel#settingsSectionTitle { font-size: 17px; font-weight: 600; color: #263C32; }
QLabel#settingsDescription { font-size: 13px; color: #71816F; }
QLabel#settingsFieldLabel { font-size: 13px; font-weight: 550; color: #5B7061; }
QLabel#settingsPath { font-size: 12px; color: #7B897F; }
QWidget#settingsPage QPushButton[settingsAction="true"] {
    font-size: 12px; padding: 7px 12px; min-height: 18px; border-radius: 7px;
}
QWidget#settingsPage QLineEdit { padding: 8px 11px; min-height: 20px; border-radius: 8px; }
QLabel#amountPreview { font-size: 29px; font-weight: 600; color: #226B53; }
QLabel#message { padding: 10px 12px; border-radius: 8px; background: #EEF3E9; color: #40684D; }
QLabel#importFeedback { padding: 12px 14px; border-radius: 8px; font-size: 13px;
    font-weight: 550; border: 1px solid #BCD1E6; background: #EDF5FC; color: #245378; }
QLabel#importFeedback[severity="success"] { background: #E8F5ED; color: #226B43; border-color: #9CCBAC; }
QLabel#importFeedback[severity="warning"] { background: #FFF5DE; color: #815409; border-color: #E7C36F; }
QLabel#importFeedback[severity="error"] { background: #FDEDEC; color: #A52C28; border-color: #E4A29D; }
QLineEdit, QComboBox { background: white; border: 1px solid #DBE4D9; border-radius: 10px;
    padding: 12px 13px; min-height: 22px; selection-background-color: #D5E8DC; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #4A8B6C; }
QLineEdit:disabled, QComboBox:disabled { color: #9CA79E; background: #F6F7F4; }
QLineEdit#amount { font-size: 25px; padding: 14px; }
QPlainTextEdit { background: white; border: 1px solid #DBE4D9; border-radius: 10px; padding: 10px; }
QProgressBar { border: 1px solid #DBE4D9; border-radius: 7px; background: #F4F7F0; text-align: center; }
QProgressBar::chunk { background: #AACBB6; border-radius: 6px; }
QComboBox::drop-down { border: none; width: 28px; }
QComboBox QAbstractItemView { background: white; selection-background-color: #E4EDE0; color: #263C32; }
QPushButton { border: 1px solid #DAE3D6; border-radius: 9px; background: white; padding: 10px 15px; color: #33634A; }
QPushButton:enabled:hover { background: #EAF1E5; }
QPushButton:checked { background: #E2EBDE; border-color: #4A8B6C; }
QPushButton:disabled { color: #929292; background: #EEEEEE; border-color: #DDDDDD; }
QPushButton#primary { background: #226B53; color: white; border: none; font-weight: 600; min-height: 26px; }
QPushButton#primary:enabled:hover { background: #1C5C46; }
QPushButton#primary:disabled { background: #EEEEEE; color: #929292; border: 1px solid #DDDDDD; }
QPushButton#navigation { text-align: left; padding: 13px 15px; border: none; background: transparent; color: #6C7E70; }
QPushButton#navigation:checked { background: #E2EBDE; color: #226B53; font-weight: 600; }
QPushButton#link { background: transparent; border: none; padding: 0; color: #226B53; }
QTableWidget { border: 1px solid #E2E9DE; border-radius: 10px; background: white; gridline-color: #EFF2EC; }
QTableWidget::item { padding: 10px; }
QTableWidget#historyTable::item { padding: 10px 4px; }
QTableWidget::item:selected { background: #E4EDDF; color: #263C32; }
QHeaderView::section { background: #F2F5EE; border: none; padding: 12px; color: #71816F; }
QDialog { background: #FCFDF9; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { width: 8px; background: transparent; }
QScrollBar::handle:vertical { background: #D1DCCB; border-radius: 4px; min-height: 30px; }
QStatusBar { background: #F8FAF5; color: #7B897F; font-size: 11px; }
"""
