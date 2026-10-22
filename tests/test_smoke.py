from chatbi import main


def test_main(capsys):
    main()
    assert "ready" in capsys.readouterr().out
