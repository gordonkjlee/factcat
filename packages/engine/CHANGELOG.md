# Changelog

## [0.5.1](https://github.com/gordonkjlee/factcat/compare/v0.5.0...v0.5.1) (2026-09-04)


### Documentation

* make the onboarding path work, and pick an installer that exists ([#78](https://github.com/gordonkjlee/factcat/issues/78)) ([327aa47](https://github.com/gordonkjlee/factcat/commit/327aa47571d4374487a3c4994969a4b465834832))

## [0.5.0](https://github.com/gordonkjlee/factcat/compare/v0.4.1...v0.5.0) (2026-09-04)


### Features

* **app:** add analyst sql wording and display case ([#46](https://github.com/gordonkjlee/factcat/issues/46)) ([0975791](https://github.com/gordonkjlee/factcat/commit/09757911959a2167e8fa87cbcb6e58c65db09650))
* **app:** add event series with typed filters and overlay ([#33](https://github.com/gordonkjlee/factcat/issues/33)) ([1f641da](https://github.com/gordonkjlee/factcat/commit/1f641da6bd08e9f4d357d7a85186a49231ad4deb))
* **app:** add hour and cyclic events time grains ([#44](https://github.com/gordonkjlee/factcat/issues/44)) ([9e069df](https://github.com/gordonkjlee/factcat/commit/9e069df847eca6b3a566554b80c781af92535770))
* **app:** add property measures, last-run results, and safer events SQL ([#27](https://github.com/gordonkjlee/factcat/issues/27)) ([88395b6](https://github.com/gordonkjlee/factcat/commit/88395b66382e77267ac757504602c499a721afa1))
* **app:** add Setup layout hints for partition and clustering ([#66](https://github.com/gordonkjlee/factcat/issues/66)) ([4b00263](https://github.com/gordonkjlee/factcat/commit/4b00263365f6908d8a3f84ebedb932ad30ae147c))
* **app:** autosave preferences and unify the save toast ([#49](https://github.com/gordonkjlee/factcat/issues/49)) ([79411f6](https://github.com/gordonkjlee/factcat/commit/79411f6aef49e00dc5e2c35058af87930cac5912))
* **app:** collapsible panes and draggable dividers on events ([#53](https://github.com/gordonkjlee/factcat/issues/53)) ([ed95aaf](https://github.com/gordonkjlee/factcat/commit/ed95aaf36658650c5aca38b9e963a58e4e764a3f))
* **app:** cycle running dots when the cat holds still ([#51](https://github.com/gordonkjlee/factcat/issues/51)) ([1e37839](https://github.com/gordonkjlee/factcat/commit/1e37839725298a19dfa44a3e0b1c6101767fa1ad))
* **app:** every page states its build, and the cap override is offered while estimating ([#70](https://github.com/gordonkjlee/factcat/issues/70)) ([04fe6f6](https://github.com/gordonkjlee/factcat/commit/04fe6f6dc1781be167171ccf6a2c479fbea3e114))
* **app:** Factcat-managed column index — build on Run, refresh, sweep, Setup section ([#68](https://github.com/gordonkjlee/factcat/issues/68)) ([80da3f8](https://github.com/gordonkjlee/factcat/commit/80da3f8f0c51cf32eb90de84bb9c88ed1f375afd))
* **app:** install warehouse extra from Setup ([#31](https://github.com/gordonkjlee/factcat/issues/31)) ([9174e0f](https://github.com/gordonkjlee/factcat/commit/9174e0fa1c41a8aa1dee0577f4b947a73f7235e1))
* **app:** launch the local chart with factcat ([#45](https://github.com/gordonkjlee/factcat/issues/45)) ([ea9acbc](https://github.com/gordonkjlee/factcat/commit/ea9acbc4bd2b15d001f7f9ab80d0eb6723c010bb))
* **app:** list Setup catalog from one step chain ([#32](https://github.com/gordonkjlee/factcat/issues/32)) ([a0245ea](https://github.com/gordonkjlee/factcat/commit/a0245ea3a836475728be3ccb8f2e8dd5608461fb))
* **app:** map chart empty states to mascot poses ([#48](https://github.com/gordonkjlee/factcat/issues/48)) ([80633fb](https://github.com/gordonkjlee/factcat/commit/80633fbd1715ba47532a0550d2627bf8ae420ab3))
* **app:** one download menu per pane with purposeful formats ([#57](https://github.com/gordonkjlee/factcat/issues/57)) ([3f1ed25](https://github.com/gordonkjlee/factcat/commit/3f1ed25a0d1114020c3ed698d4ba1a71d686ade0))
* **app:** override means override, and Run refuses over cap ([#64](https://github.com/gordonkjlee/factcat/issues/64)) ([eea326a](https://github.com/gordonkjlee/factcat/commit/eea326aadf081c691e93b303555b3cd42f6a071b))
* **app:** per-slot breakdown value controls (Value at, If missing, Fill from) ([#63](https://github.com/gordonkjlee/factcat/issues/63)) ([1850bce](https://github.com/gordonkjlee/factcat/commit/1850bce325af6e8beb196fb173fb560d2a7c9f98))
* **app:** persist catalog lists and split event-name refresh ([#47](https://github.com/gordonkjlee/factcat/issues/47)) ([1ee3d77](https://github.com/gordonkjlee/factcat/commit/1ee3d77d2ca151e7e53fe5afdb47eff702bd16c8))
* **app:** prune catalog windows and cache event names ([#43](https://github.com/gordonkjlee/factcat/issues/43)) ([fa9a6f0](https://github.com/gordonkjlee/factcat/commit/fa9a6f04a2b353d4da82f8011a7d175672b68722))
* **app:** sectioned config column and grouped fill-from options ([#65](https://github.com/gordonkjlee/factcat/issues/65)) ([d7fae98](https://github.com/gordonkjlee/factcat/commit/d7fae989c030c06bc7d08d4e9e61677b10c32c36))
* **app:** separate user preferences from project setup ([#36](https://github.com/gordonkjlee/factcat/issues/36)) ([46ac06c](https://github.com/gordonkjlee/factcat/commit/46ac06cc3f1046c9e27524fdc200774252f3ea04))
* **app:** week start and reporting timezone belong to the report builder ([#72](https://github.com/gordonkjlee/factcat/issues/72)) ([97d142e](https://github.com/gordonkjlee/factcat/commit/97d142e672dd18b8a69d9bd7799e058e9c580c1e))
* **app:** width flyout on the panel handle and a labelled row cap ([#55](https://github.com/gordonkjlee/factcat/issues/55)) ([c30e0c2](https://github.com/gordonkjlee/factcat/commit/c30e0c21b0990741858b7a7debb0b00af5a6df84))
* **dialects:** use approx top-K for breakdown labels ([#34](https://github.com/gordonkjlee/factcat/issues/34)) ([5dcb75d](https://github.com/gordonkjlee/factcat/commit/5dcb75d147ed515946153c09ef940c4114332cf9))
* **engine:** add Snowflake adapter and Setup warehouse kind ([#30](https://github.com/gordonkjlee/factcat/issues/30)) ([74bc1cb](https://github.com/gordonkjlee/factcat/commit/74bc1cb78e64fdbf6862cc7f4ae0f5f29f9f88a6))
* **engine:** allow multiple Events breakdowns ([#41](https://github.com/gordonkjlee/factcat/issues/41)) ([37d1120](https://github.com/gordonkjlee/factcat/commit/37d11201b160e6487a6084fecce72965dee21708))
* **engine:** per-column breakdown value semantics (carried, bounds, backfill) ([#62](https://github.com/gordonkjlee/factcat/issues/62)) ([66f9989](https://github.com/gordonkjlee/factcat/commit/66f998919dfc702abd9377aca55131a1259bcf65))
* **engine:** read breakdown values from a caller relation via values_table ([#67](https://github.com/gordonkjlee/factcat/issues/67)) ([f0a922b](https://github.com/gordonkjlee/factcat/commit/f0a922b21f5bc601910ea1a9fe686e102864643a))


### Bug Fixes

* **app:** a named timezone or Unix epoch could not chart at all ([#71](https://github.com/gordonkjlee/factcat/issues/71)) ([772ba75](https://github.com/gordonkjlee/factcat/commit/772ba75cfdc0999c06bb9a4ce4abbe01a1198c14))
* **app:** always show export chrome, never cache HTML ([#59](https://github.com/gordonkjlee/factcat/issues/59)) ([ac75cb0](https://github.com/gordonkjlee/factcat/commit/ac75cb0dd16ed1ff64a0e06cf860fc65770d215d))
* **app:** give every panel a single scroll owner ([#42](https://github.com/gordonkjlee/factcat/issues/42)) ([5ecbf59](https://github.com/gordonkjlee/factcat/commit/5ecbf59b8093901bed0bfed075f5e0763f995c9b))
* **app:** hide refresh while loading and keep spinners turning ([#50](https://github.com/gordonkjlee/factcat/issues/50)) ([cead05d](https://github.com/gordonkjlee/factcat/commit/cead05d4423dc5765e4f1246b79b0305cc7968a6))
* **app:** identical 8px gaps between cards in every collapse state ([#61](https://github.com/gordonkjlee/factcat/issues/61)) ([576e6fe](https://github.com/gordonkjlee/factcat/commit/576e6fedba26f091ff917acb13e1f91cba38cae4))
* **app:** keep chart series colours independent of theme ([#40](https://github.com/gordonkjlee/factcat/issues/40)) ([5974a7f](https://github.com/gordonkjlee/factcat/commit/5974a7f440e52beb83a4e6fd8a581f29030239dd))
* **app:** never queue a run behind the estimate, trim run chrome ([#52](https://github.com/gordonkjlee/factcat/issues/52)) ([af8d497](https://github.com/gordonkjlee/factcat/commit/af8d497cade481c73f8905e0fad769a9a0aa945f))
* **app:** one geometry for every pane-head icon control ([#58](https://github.com/gordonkjlee/factcat/issues/58)) ([5f8e968](https://github.com/gordonkjlee/factcat/commit/5f8e968de09f621b274d368276b63817955c8ad5))
* **app:** page-scroll events with sticky toolbar and slim divider ([#54](https://github.com/gordonkjlee/factcat/issues/54)) ([34ee3a5](https://github.com/gordonkjlee/factcat/commit/34ee3a520307462ec320d762b48d2f183ee166c6))
* **app:** pane popovers no longer ride 6px below their neighbours ([#60](https://github.com/gordonkjlee/factcat/issues/60)) ([ccf9b87](https://github.com/gordonkjlee/factcat/commit/ccf9b87f4882ed3492c5b739bc08d0f9da99584b))
* **app:** Setup no longer points at Preferences for the calendar settings ([#74](https://github.com/gordonkjlee/factcat/issues/74)) ([1464258](https://github.com/gordonkjlee/factcat/commit/14642589cca809ca3be28056a125e41bdb65794e))
* **app:** stop re-querying event names on every events visit ([#56](https://github.com/gordonkjlee/factcat/issues/56)) ([24bee8e](https://github.com/gordonkjlee/factcat/commit/24bee8e539ae3ea20bb1c0ea0e987acf3325f34e))
* **app:** the column index registry lives in .factcat.json, written per column ([#73](https://github.com/gordonkjlee/factcat/issues/73)) ([3f6fe09](https://github.com/gordonkjlee/factcat/commit/3f6fe09d246a8ee4a939166f1d0fd6f2fd07bf70))
* **app:** the sweep no longer destroys the index the chart is asking for ([#75](https://github.com/gordonkjlee/factcat/issues/75)) ([936747a](https://github.com/gordonkjlee/factcat/commit/936747ac463512d2cbd765fcc0fe4eb1159e7037))
* **dialects:** nest Spark explode outside approx_top_k ([#35](https://github.com/gordonkjlee/factcat/issues/35)) ([5891061](https://github.com/gordonkjlee/factcat/commit/5891061ae654782fde91842ab67120b1120a058c))
* **engine:** lower Snowflake result columns, and say the adapter is experimental ([#77](https://github.com/gordonkjlee/factcat/issues/77)) ([7f5315b](https://github.com/gordonkjlee/factcat/commit/7f5315bc4dddcc49b8aebbf02cfb77f6b08d738a))
* **engine:** the (other) fold no longer clashes types on a numeric breakdown ([#69](https://github.com/gordonkjlee/factcat/issues/69)) ([a3e8421](https://github.com/gordonkjlee/factcat/commit/a3e842183e0edb087ec69410edd20169205fb2ec))


### Documentation

* **pitch:** position as an Amplitude and Mixpanel alternative ([#38](https://github.com/gordonkjlee/factcat/issues/38)) ([27a2e02](https://github.com/gordonkjlee/factcat/commit/27a2e0244d5201ad61895a55fe82384b60fd9059))
* **readme:** reframe the problem as modelling decisions ([#39](https://github.com/gordonkjlee/factcat/issues/39)) ([769719c](https://github.com/gordonkjlee/factcat/commit/769719c0f0655e8ca51cba8720cd6c692369af4f))

## [0.4.1](https://github.com/gordonkjlee/factcat/compare/v0.4.0...v0.4.1) (2026-08-29)


### Bug Fixes

* **app:** include Setup guide once in the wheel ([#23](https://github.com/gordonkjlee/factcat/issues/23)) ([a4d6a55](https://github.com/gordonkjlee/factcat/commit/a4d6a55b6183cbfc7ac618c0f2ad8207a3c3f7de))

## [0.4.0](https://github.com/gordonkjlee/factcat/compare/v0.3.0...v0.4.0) (2026-08-29)


### Features

* **app:** add mark, favicon, and colour tokens ([#17](https://github.com/gordonkjlee/factcat/issues/17)) ([927775a](https://github.com/gordonkjlee/factcat/commit/927775a60756c363f638c97af435c53f006a857a))
* **app:** Events workspace with setup catalog and query safety ([#16](https://github.com/gordonkjlee/factcat/issues/16)) ([aba5ab2](https://github.com/gordonkjlee/factcat/commit/aba5ab2b36645d0cbf0e048c7541be5dcf9e83aa))
* **app:** map reporting timezone into Events SQL ([#21](https://github.com/gordonkjlee/factcat/issues/21)) ([9e75bd5](https://github.com/gordonkjlee/factcat/commit/9e75bd5c75f018b7cedf7a7d50d05dbc0ed91263))
* **engine:** add Events breakdowns with top-N and (other) ([#20](https://github.com/gordonkjlee/factcat/issues/20)) ([ee78aba](https://github.com/gordonkjlee/factcat/commit/ee78aba34c3a3d6cff6b039eed8a7581b9346476))

## [0.3.0](https://github.com/gordonkjlee/factcat/compare/v0.2.0...v0.3.0) (2026-08-28)


### Features

* **app:** map tables and columns from BigQuery metadata ([#12](https://github.com/gordonkjlee/factcat/issues/12)) ([4ce3caa](https://github.com/gordonkjlee/factcat/commit/4ce3caa505af8fde3272e50a5b24e7490b47fe41))

## [0.2.0](https://github.com/gordonkjlee/factcat/compare/v0.1.0...v0.2.0) (2026-08-28)


### Features

* **engine:** add Events time series with Total and Uniques ([#6](https://github.com/gordonkjlee/factcat/issues/6)) ([1b97b45](https://github.com/gordonkjlee/factcat/commit/1b97b453627ea9e7cbb74088064ce87713e3b7d3))
* **engine:** add warehouse execute adapters with BigQuery first ([#5](https://github.com/gordonkjlee/factcat/issues/5)) ([c9b2922](https://github.com/gordonkjlee/factcat/commit/c9b292285dd6d5776bd5b7331862f2ded9822a80))

## Changelog
