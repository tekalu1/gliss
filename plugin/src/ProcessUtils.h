#pragma once

namespace gliss
{

/** このプロセスの ID。 */
int currentProcessId();

/** 指定のプロセスが今も動いているか（WebView2 のユーザーデータの、前のプロセスが残したフォルダの掃除に使う）。 */
bool isProcessRunning (int processId);

} // namespace gliss
