"""GitHub完結型RPGエンジン。

不変則: このパッケージに世界の固有名詞を書かない。世界の個性は world/world.json のみが持つ。
ロジックは純粋関数、I/O(git・GitHub API・AI呼び出し)は境界モジュール
(save_io / gh_api / gitops / ai_client)に隔離する。
"""
