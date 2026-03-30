# Українською

Аддон для **Blender 4.1** для експорту **.OxModel** файлів з гри **Star Control: Origins**; моделі матимуть правильну UV-мапу та кістки


Усі .OxModel файли лежать у теці `\Star Control - Origins\Assets\CookedData`

Віпдовідні текстури у .dds форматі будуть у `\Star Control - Origins\Assets\CookedData\`; матеріали зберігаються у .Palette файлах, нажаль моделі не мають прямих референсів на матеріали, але якщо перенести .OxModel  у теку .Palette та відповідно перейменувати (щоб назви співпадали), аддон зможе зробити відповідний матеріал

Відомі проблеми:
1. Аддон не ділить модель по матеріалам; у грі небагато моделей, що потребують різні матеріали під різні частини мешей, втім у такому випадку доведеться вручну їх розділяти, себто: ctrl+M -> L на відпповідний фрагмент -> Assing до нового матеріалу
2. Аддон не може накласти матеріал без ручного перенесення до .Palette
3. Аддон був зроблений суто під версію Bledner 4.1
4. Скелет не буде мати Auti IK та X-Axis Mirror

# English

Add-on for **Blender 4.1** for exporting **.OxModel** files from the game **Star Control: Origins**; models will have correct UV maps and bones


All .OxModel files are located in `\Star Control - Origins\Assets\CookedData`

The corresponding textures in .dds format can be found in `\Star Control - Origins\Assets\CookedData\`; materials are stored in .Palette files, unfortunately, the models do not have direct references to the materials, but if you move the .OxModel file to the corresponding folder with .Palette and rename it accordingly (so that the names match), the addon will be able to generate the corresponding material

Known issues:
1. The add-on does not split models by material; there are few models in the game that require different materials for different parts of the mesh, but in such cases, you will have to split them manually, ie: Ctrl+M -> L on the corresponding fragment -> Assign to a new material
2. The add-on cannot apply a material without manually matching it to the .Palette
3. The add-on was made exclusively for Bledner version 4.1
4. The Armature will not have Auti IK or X-Axis Mirror 

# Archive (Готовий архів)
Archive with already exported key models
> [https://mega.nz/folder/zR1mVYbS#pC3Mnw2vyCpr-MtV-9jB4g](https://mega.nz/folder/zR1mVYbS#pC3Mnw2vyCpr-MtV-9jB4g)
